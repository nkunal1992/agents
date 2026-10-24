"""Local persistent index: SQLite metadata + FTS5 keyword + vector blobs.

One DB per scope (site+account+root). Crawl output is written to job-scoped
staging tables; `publish` swaps it into the live tables in a single
transaction, so a running or interrupted refresh never disturbs the snapshot
readers see. Transient-failed pages carry their previous live rows forward.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- vec_chunks is created lazily by _ensure_ann() — it needs sqlite-vec
-- (optional ext) AND the live vector dim (model-specific).
CREATE TABLE IF NOT EXISTS pages (
  page_id TEXT PRIMARY KEY, parent_id TEXT, title TEXT, url TEXT,
  level INTEGER, version TEXT, fetched_at REAL, content_hash TEXT,
  warnings TEXT, active INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY, page_id TEXT, heading TEXT, kind TEXT,
  ordinal INTEGER, text TEXT, active INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS vectors (chunk_id TEXT PRIMARY KEY, vec BLOB);
CREATE TABLE IF NOT EXISTS sparse_vectors (chunk_id TEXT PRIMARY KEY, payload BLOB);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED, text);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, root_page TEXT, state TEXT, created_at REAL,
  queue TEXT, errors TEXT);
-- per-term document frequency over the LIVE sparse vectors — rebuilt inside
-- every publish transaction so BM25 idf always matches the snapshot
CREATE TABLE IF NOT EXISTS term_stats (term_id INTEGER PRIMARY KEY, df INTEGER NOT NULL);
-- job-scoped staging: crawl writes land here, never in the live tables
CREATE TABLE IF NOT EXISTS pages_stage (
  job_id TEXT NOT NULL, page_id TEXT NOT NULL, parent_id TEXT, title TEXT,
  url TEXT, level INTEGER, version TEXT, fetched_at REAL, content_hash TEXT,
  warnings TEXT, PRIMARY KEY(job_id, page_id));
CREATE TABLE IF NOT EXISTS chunks_stage (
  job_id TEXT NOT NULL, chunk_id TEXT NOT NULL, page_id TEXT, heading TEXT,
  kind TEXT, ordinal INTEGER, text TEXT, PRIMARY KEY(job_id, chunk_id));
CREATE TABLE IF NOT EXISTS vectors_stage (
  job_id TEXT NOT NULL, chunk_id TEXT NOT NULL, vec BLOB,
  PRIMARY KEY(job_id, chunk_id));
CREATE TABLE IF NOT EXISTS sparse_vectors_stage (
  job_id TEXT NOT NULL, chunk_id TEXT NOT NULL, payload BLOB,
  PRIMARY KEY(job_id, chunk_id));
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_stage USING fts5(
  job_id UNINDEXED, chunk_id UNINDEXED, text);
-- cross-process worker lease: exactly one MCP server process (one IDE
-- window) runs a worker on a job; heartbeat goes stale on crash/close
CREATE TABLE IF NOT EXISTS job_lease (
  job_id TEXT PRIMARY KEY, owner TEXT, pid INTEGER, heartbeat REAL);
"""

_ANN_SCOPE_MIN = 100_000   # scopes above this use vec_chunks (ANN) —
                            # the dense cache matrix would cost the RAM
                            # the cap was guarding (#251)

_STAGE_TABLES = ("pages_stage", "chunks_stage", "vectors_stage",
                 "sparse_vectors_stage", "chunks_fts_stage")
# writes that bypass the job system (tests, tools) use this stage owner
SCRATCH_JOB = ""  # job_id for writes made outside a crawl job (tests, tools)


class StaleJobError(RuntimeError):
    """A crawl job tried to write after being superseded or abandoned —
    only a 'running' job may stage pages (R13)."""


class _SearchCache:
    """In-memory retrieval structures for one published snapshot.

    Built once per snapshot epoch: the dense matrix (float16 to halve the
    footprint), an inverted sparse index (term → posting rows+weights),
    the active-id set for FTS filtering, BM25 stats, and the page map.
    Without it every search query re-read the whole scope from SQLite —
    hundreds of MB of transient allocations per question on big scopes.
    """
    __slots__ = ("chunk_page", "dense", "df", "epoch", "ids", "ids_set",
                 "n_docs", "pages", "row", "sparse_idx")

    def __init__(self, epoch):
        self.epoch = epoch
        self.ids: list[str] = []
        self.ids_set: set[str] = set()
        self.row: dict[str, int] = {}
        self.chunk_page: dict[str, str] = {}
        self.dense = np.zeros((0, 0), dtype=np.float16)
        self.sparse_idx: dict[int, tuple] = {}
        self.df: dict[int, int] = {}
        self.n_docs = 0
        self.pages: dict[str, dict] = {}


def _batches(ids: list[str], size: int = 5000):
    for i in range(0, len(ids), size):
        yield ids[i:i + size]

class _LockedRetry:
    """sqlite3 database context manager that retries `database is locked`.

    WAL writers get SQLITE_BUSY_SNAPSHOT instantly — connect(timeout=30)
    only covers lock contention, not checkpoint starvation behind a
    reader's snapshot (real failure on loaded Windows CI runners). A
    small bounded retry turns the snapshot conflict into a transient.
    """
    # ~10s total — must outlast a sibling install's publish txn now that
    # separate venvs share the same index files (#257); the pre-#257 ~1.5s
    # budget only covered checkpoint starvation
    _TRIES = 9
    _BACKOFF = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 3.0)

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def __enter__(self):
        for attempt in range(self._TRIES):
            try:
                self.db.execute("BEGIN IMMEDIATE")
                return self.db
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or attempt == self._TRIES - 1:
                    raise
                time.sleep(self._BACKOFF[attempt])
        return self.db                       # pragma: no cover

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.db.commit()
        else:
            self.db.rollback()
        return False


class Index:
    def __init__(self, path: Path, readonly: bool = False):
        self._path = path
        self._readonly = readonly
        # read-only shadow connections, one PER THREAD: WAL lets a second
        # reader see the last-committed snapshot without touching the
        # writer's transaction — light tools (status/crawl_step/evidence)
        # must never queue behind a publish sweep or a cache rebuild
        # (#188). A single shared conn can't run cursors from two threads
        # safely — publish's cache-warm thread proved that (#255)
        self._rdb_local = threading.local()
        self._rconns: set[sqlite3.Connection] = set()
        self._rlock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        # timeout: retry transient locks (AV scan, second process, WAL
        # checkpoint) instead of failing instantly; check_same_thread: the
        # crawl worker and tool-call threads share this one connection —
        # `self.lock` serializes every access so writes never interleave
        try:
            last_err = None
            for attempt in range(6):
                # a killed owner leaves a hot WAL journal — on Windows the
                # recovery/probe can transiently fail while the dead
                # process's file handles finish releasing. Retry only the
                # transient class (locked/CANTOPEN/I/O → OperationalError);
                # malformed/not-a-database stays a fast SetupError.
                try:
                    if readonly:
                        # proxy mode (#189): every write attempt on this
                        # connection raises OperationalError — read-only is
                        # enforced by SQLite, not by convention
                        self.db = sqlite3.connect(
                            f"file:{path}?mode=ro", uri=True, timeout=30,
                            check_same_thread=False)
                    else:
                        self.db = sqlite3.connect(path, timeout=30,
                                                  check_same_thread=False)
                    self.db.execute(
                        "SELECT name FROM sqlite_master LIMIT 1").fetchone()
                    break
                except sqlite3.OperationalError as e:
                    last_err = e
                    if attempt == 5:
                        raise
                    time.sleep((0.05, 0.1, 0.2, 0.4, 0.8)[attempt])
            else:                                   # pragma: no cover
                raise last_err
        except sqlite3.DatabaseError as e:
            # close before raising — Windows keeps the file locked otherwise
            from contextlib import suppress
            with suppress(Exception):
                self.db.close()
            from .settings import SetupError
            raise SetupError(
                f"index unreadable at {path} — if a different package "
                "version wrote it, align every install on one version; "
                "otherwise run `clean` and re-crawl") from e
        self.lock = threading.RLock()
        self._cache: _SearchCache | None = None
        self._cache_cv = threading.Condition(self.lock)
        self._building_cache = False
        self._warm_threads: set[threading.Thread] = set()
        self._ann: bool | None = None   # None=untried, True=ready, False=no ext
        # strict schema gate (#257): separate IDE installs share the index
        # files — a version-skewed second install must refuse cleanly, not
        # misread a newer layout. Pre-production: no migrations, one rule.
        # A concurrent first-open can observe the file mid-creation (DDL
        # commits before the meta row lands) — wait briefly before the
        # strict checks so that peer isn't refused as a legacy index.
        deadline = time.monotonic() + 2.0
        while True:
            has_meta = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='meta'").fetchone() is not None
            row = self.db.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone() if has_meta else None
            stored = row[0] if row is not None else None
            has_tables = has_meta or self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "LIMIT 1").fetchone() is not None
            if stored is not None or not has_tables or \
                    time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        if has_meta:
            if stored != str(SCHEMA_VERSION):
                self.db.close()
                from .settings import SetupError
                raise SetupError(
                    f"index at {path} "
                    + ("predates schema versioning" if stored is None else
                       f"was written by schema v{stored}")
                    + f" — this build expects v{SCHEMA_VERSION}. Run every "
                    "install on the same package version, or `clean` and "
                    "re-crawl")
        elif has_tables:
            # tables but no meta — a pre-versioning index; refuse rather
            # than silently adopt a layout this build can't trust
            self.db.close()
            from .settings import SetupError
            raise SetupError(
                f"index at {path} predates schema versioning — this build "
                f"expects v{SCHEMA_VERSION}. Run `clean` and re-crawl")
        elif readonly:
            self.db.close()
            from .settings import SetupError
            raise SetupError(
                f"index at {path} holds no schema — run `clean` and re-crawl")
        if readonly:
            # proxy mode (#189): the main conn already is ro — every
            # thread shares it, matching the pre-thread-local behavior
            self._rdb_local.conn = self.db
            return
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),))
            # an uncommitted INSERT holds the write lock for the connection's
            # lifetime and deadlocks every other connection (#68)
            self.db.commit()
            # indexes built before term_stats existed: rebuild once on open
            # so BM25 idf works immediately, not only after the next crawl
            if not self.db.execute(
                    "SELECT 1 FROM term_stats LIMIT 1").fetchone() and \
                    self.db.execute(
                    "SELECT 1 FROM sparse_vectors LIMIT 1").fetchone():
                self._rebuild_term_stats()
                self.db.commit()

    def close(self) -> None:
        """Release all connections — Windows refuses file deletion while
        a read conn is still open (e2e cleanup hit WinError 32)."""
        # a publish's cache-warm thread iterates a registered read conn —
        # closing it mid-query frees the buffer numpy is viewing (the
        # frombuffer zero-copy path) → use-after-free segfault. The build
        # is one bounded scan; joining is always finite (#257)
        for t in list(self._warm_threads):
            t.join()
        with self._rlock:
            conns = list(self._rconns) + [self.db]
            self._rconns.clear()
        for conn in conns:
            if conn is not None:
                try:
                    conn.close()
                except Exception:           # noqa: BLE001,S110 — close
                    pass                     # is best-effort at teardown
        self._rdb_local.conn = None

    def _open_read_conn(self) -> sqlite3.Connection:
        """Fresh read-only WAL connection — registered so close() can
        reach it from the owner thread at teardown."""
        conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True,
                               timeout=5, check_same_thread=False)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if mode != "wal":
            from .settings import SetupError
            conn.close()
            raise SetupError(
                f"index journal_mode is {mode!r}, expected wal")
        with self._rlock:
            self._rconns.add(conn)
        return conn

    def _read(self) -> sqlite3.Connection:
        """This thread's read-only connection — WAL snapshot semantics
        mean a mid-publish read simply sees the previous snapshot."""
        conn = getattr(self._rdb_local, "conn", None)
        if conn is None:
            conn = self._open_read_conn()
            self._rdb_local.conn = conn
        return conn

    # ---- jobs ----
    def create_job(self, job_id: str, root_page: str) -> None:
        with self.lock, _LockedRetry(self.db):
            self.db.execute(
                "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?)",
                (job_id, root_page, "queued", time.time(),
                 json.dumps([root_page]), "[]"))
            self._purge_dead_stage()

    def job(self, job_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT root_page,state,queue,errors FROM jobs WHERE job_id=?",
                (job_id,)).fetchone()
        if not row:
            return None
        return {"root_page": row[0], "state": row[1],
                "queue": json.loads(row[2]), "errors": json.loads(row[3])}

    def running_live_job(self, stale_s: float = 30.0) -> str | None:
        """A running job whose lease heartbeat is fresh — its worker is
        alive. start_crawl dedup: a second call on a running scope
        returns this job instead of abandoning it (field case: agent
        issued Start crawl twice → first job abandoned mid-flight).
        Orphaned jobs (dead worker, stale lease) are NOT returned —
        abandon + resume handles those."""
        with self._rlock:
            row = self._read().execute(
                "SELECT j.job_id FROM jobs j "
                "JOIN job_lease l ON l.job_id = j.job_id "
                "WHERE j.state = 'running' AND l.heartbeat > ?",
                (time.time() - stale_s,)).fetchone()
        return row[0] if row else None

    def abandon_running_jobs(self) -> None:
        """A new crawl supersedes any unfinished one (e.g. an earlier failed
        attempt) so it can't leave coverage reported as partial forever."""
        with self.lock, _LockedRetry(self.db):
            self.db.execute("UPDATE jobs SET state='abandoned' WHERE state='running'")

    def latest_job(self) -> dict | None:
        with self._rlock:
            row = self._read().execute(
                "SELECT job_id,state,errors FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        return row and {"job_id": row[0], "state": row[1], "errors": json.loads(row[2])}

    def save_job(self, job_id: str, state: str, queue: list, errors: list) -> None:
        with self.lock, _LockedRetry(self.db):
            self.db.execute(
                "UPDATE jobs SET state=?, queue=?, errors=? WHERE job_id=?",
                (state, json.dumps(queue), json.dumps(errors), job_id))

    def meta_get(self, key: str) -> str | None:
        with self._rlock:
            row = self._read().execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def job_progress(self, job_id: str) -> dict:
        """Snapshot the agent can poll while a background worker crawls."""
        job = self.job(job_id)
        if job is None:
            raise KeyError(job_id)
        seen = self.meta_get(f"seen:{job_id}")
        out = {"job_id": job_id, "state": job["state"],
               "remaining": len(job["queue"]),
               "pages_seen": len(json.loads(seen)) if seen else 0,
               "errors": job["errors"]}
        pub = self.meta_get(f"publish:{job_id}")
        if pub:
            out["publish"] = json.loads(pub)   # staged/deleted/kept (#176)
        st = self.meta_get(f"stats:{job_id}")
        if st:
            out["stats"] = json.loads(st)   # per-phase crawl timing
        return out

    def meta_set(self, key: str, value: str) -> None:
        with self.lock, _LockedRetry(self.db):
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                            (key, value))

    def meta_del(self, key: str) -> None:
        with self.lock, _LockedRetry(self.db):
            self.db.execute("DELETE FROM meta WHERE key=?", (key,))

    # ---- evidence-set persistence (#231) ----
    # _evidence_sets is process memory — a daemon restart must not turn
    # verify_citations into 'unknown evidence set'. Sets persist here,
    # bounded to the last 16 per scope, inside the scope's own index so a
    # set_id is inherently scope-bound.
    _EVSET_KEEP = 16

    def persist_evset(self, set_id: str, chunk_ids: list[str],
                      epoch) -> None:
        try:
            raw = self.meta_get("evsets")
            sets = json.loads(raw) if raw else {}
            sets[set_id] = {"c": chunk_ids, "e": epoch}
            while len(sets) > self._EVSET_KEEP:
                sets.pop(next(iter(sets)))
            self.meta_set("evsets", json.dumps(sets))
        except Exception:                           # noqa: BLE001,S110 —
            pass                                    # readonly proxy —
        # the daemon-side write is what counts anyway

    def evset_rebind(self, set_id: str) -> dict | None:
        """Rebuild a lost evidence set from its persisted shadow — chunk
        rows come straight from the index, so this needs no network and
        no embeddings (#231)."""
        try:
            sets = json.loads(self.meta_get("evsets") or "{}")
            p = sets.get(set_id)
            if not p:
                return None
            return {"ev": self.chunks_by_ids(p["c"]), "epoch": p["e"]}
        except Exception:                           # noqa: BLE001
            return None

    # ---- cross-process worker lease ----
    # Every MCP server process (each IDE window spawns one) could otherwise
    # run a worker on the same job — double CPU/RAM and write contention.
    # The lease row is the fence; a heartbeat older than `stale_s` (crash,
    # closed window) or a dead pid on this host can be taken over.
    def acquire_lease(self, job_id: str, owner: str, pid: int,
                      stale_s: float = 30.0) -> bool:
        import socket
        try:
            import psutil
        except ImportError:
            psutil = None
        host = socket.gethostname()
        now = time.time()
        with self.lock, _LockedRetry(self.db):
            row = self.db.execute(
                "SELECT owner,pid,heartbeat FROM job_lease WHERE job_id=?",
                (job_id,)).fetchone()
            if row is not None:
                if row[0] == owner:
                    return True          # already ours (resume after retry)
                stale = (row[2] or 0) < now - stale_s
                dead = False
                if not stale and psutil is not None:
                    try:
                        # daemon owners are 'daemon:host:pid:nonce' — the
                        # prefix must come off or the host compare never
                        # matches and a killed daemon's lease blocks
                        # takeover until the heartbeat goes stale (#257)
                        owner_host = row[0].rsplit(":", 2)[0] \
                            .removeprefix("daemon:")
                        dead = owner_host == host and \
                            not psutil.pid_exists(int(row[1]))
                    except Exception:    # noqa: BLE001 — can't verify → only
                        dead = False     # staleness may hand it over
                if not (stale or dead):
                    return False
            self.db.execute(
                "INSERT OR REPLACE INTO job_lease VALUES (?,?,?,?)",
                (job_id, owner, pid, now))
            return True

    def lease_owner(self, job_id: str) -> str | None:
        """Who holds the job's worker lease — feeds the 'the daemon /
        another process' label in progress payloads (#186/#189)."""
        with self._rlock:
            row = self._read().execute(
                "SELECT owner FROM job_lease WHERE job_id=?",
                (job_id,)).fetchone()
        return row[0] if row else None

    def heartbeat(self, job_id: str, owner: str) -> str | None:
        """Refresh our lease; returns the job's current state so the worker
        notices cross-process cancel/supersede. None when not our lease."""
        now = time.time()
        with self.lock, _LockedRetry(self.db):
            cur = self.db.execute(
                "UPDATE job_lease SET heartbeat=? WHERE job_id=? AND owner=?",
                (now, job_id, owner))
            if cur.rowcount == 0:
                return None              # lease was taken over or released
            row = self.db.execute(
                "SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            return row[0] if row else "missing"

    def release_lease(self, job_id: str, owner: str) -> None:
        with self.lock, _LockedRetry(self.db):
            self.db.execute(
                "DELETE FROM job_lease WHERE job_id=? AND owner=?",
                (job_id, owner))

    def _purge_dead_stage(self) -> None:
        """Drop staging rows of jobs that can never publish (abandoned/done/
        error). Must run inside a transaction on self.db."""
        for t in _STAGE_TABLES:
            self.db.execute(
                f"DELETE FROM {t} WHERE job_id != ? AND job_id NOT IN "  # nosec B608
                "(SELECT job_id FROM jobs WHERE state IN ('queued','running'))",
                (SCRATCH_JOB,))
        self.db.execute(
            "DELETE FROM meta WHERE key LIKE 'build:%' AND substr(key,7) != ? "
            "AND substr(key,7) NOT IN "
            "(SELECT job_id FROM jobs WHERE state IN ('queued','running'))",
            (SCRATCH_JOB,))

    # ---- content ----
    def upsert_page(self, page: dict, chunks: list, vecs: np.ndarray | None,
                    sparse: list[dict[int, float]] | None = None,
                    job_id: str = SCRATCH_JOB) -> None:
        """Stage a crawled page under its job. Live tables are NEVER touched
        here — readers keep the last published snapshot until publish()."""
        with self.lock, _LockedRetry(self.db):
            if job_id != SCRATCH_JOB:
                row = self.db.execute(
                    "SELECT state FROM jobs WHERE job_id=?",
                    (job_id,)).fetchone()
                if row is None or row[0] != "running":
                    raise StaleJobError(
                        f"job {job_id} is {row[0] if row else 'gone'} — "
                        "a superseded crawl cannot write")
                # R14: the first staged write binds the job's build
                # identity — a resumed crawl producing different-dimension
                # vectors can never publish a mixed-dimension index
                if vecs is not None and len(vecs):
                    dim = int(np.asarray(vecs).shape[1])
                    bkey = f"build:{job_id}"
                    brow = self.db.execute(
                        "SELECT value FROM meta WHERE key=?",
                        (bkey,)).fetchone()
                    if brow is None:
                        self.db.execute("INSERT INTO meta VALUES (?,?)",
                                        (bkey, str(dim)))
                    elif int(brow[0]) != dim:
                        raise StaleJobError(
                            f"job {job_id} bound to {brow[0]}-dim vectors — "
                            f"resumed with {dim}-dim; start a fresh crawl")
            self.db.execute(
                "INSERT OR REPLACE INTO pages_stage VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, page["page_id"], page.get("parent_id"), page["title"],
                 page["url"], page.get("level", 0), page.get("version"),
                 time.time(), page.get("content_hash", ""),
                 json.dumps(page.get("warnings", []))))
            old = [r[0] for r in self.db.execute(
                "SELECT chunk_id FROM chunks_stage WHERE job_id=? AND page_id=?",
                (job_id, page["page_id"]))]
            for cid in old:
                self.db.execute(
                    "DELETE FROM chunks_fts_stage WHERE job_id=? AND chunk_id=?",
                    (job_id, cid))
                self.db.execute(
                    "DELETE FROM vectors_stage WHERE job_id=? AND chunk_id=?",
                    (job_id, cid))
                self.db.execute(
                    "DELETE FROM sparse_vectors_stage WHERE job_id=? AND chunk_id=?",
                    (job_id, cid))
            self.db.execute(
                "DELETE FROM chunks_stage WHERE job_id=? AND page_id=?",
                (job_id, page["page_id"]))
            for i, c in enumerate(chunks):
                self.db.execute(
                    "INSERT INTO chunks_stage VALUES (?,?,?,?,?,?,?)",
                    (job_id, c.chunk_id, page["page_id"], c.heading_path,
                     c.kind, c.ordinal, c.text))
                self.db.execute(
                    "INSERT INTO chunks_fts_stage VALUES (?,?,?)",
                    (job_id, c.chunk_id, c.text))
                if vecs is not None and i < len(vecs):
                    self.db.execute(
                        "INSERT INTO vectors_stage VALUES (?,?,?)",
                        (job_id, c.chunk_id,
                         vecs[i].astype(np.float32).tobytes()))
                if sparse is not None and i < len(sparse) and sparse[i]:
                    # bm25 ids are int64 — int32 silently wraps high hashes
                    idx_arr = np.fromiter(sparse[i].keys(), dtype=np.int64)
                    val_arr = np.fromiter(sparse[i].values(), dtype=np.float32)
                    self.db.execute(
                        "INSERT INTO sparse_vectors_stage VALUES (?,?,?)",
                        (job_id, c.chunk_id,
                         idx_arr.tobytes() + val_arr.tobytes()))

    def _var_batch(self) -> int:
        """Actual per-statement variable limit of THIS connection, minus
        margin — batch `IN` lists under it (R15: some SQLite builds cap at
        32,766, below the declared chunk cap)."""
        try:
            return max(1, self.db.getlimit(
                sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) - 8)
        except AttributeError:
            return 999  # sqlite3 without getlimit — conservative batch

    def sparse_for(self, chunk_ids: list[str]) -> dict[str, dict[int, float]]:
        if not chunk_ids:
            return {}
        out = {}
        with self._rlock:
            step = self._var_batch()
            for off in range(0, len(chunk_ids), step):
                part = chunk_ids[off:off + step]
                q = ",".join("?" * len(part))
                for cid, blob in self._read().execute(
                        f"SELECT chunk_id,payload FROM sparse_vectors "  # nosec B608
                        f"WHERE chunk_id IN ({q})", part):
                    # layout: int64[n] indices + float32[n] values → n = len/12
                    k = len(blob) // 12
                    idx = np.frombuffer(blob[:8 * k], dtype=np.int64)
                    vals = np.frombuffer(blob[8 * k:], dtype=np.float32)
                    out[cid] = dict(zip(idx.tolist(), vals.tolist()))
        return out

    def model_id(self) -> str | None:
        return self.meta_get("embed_model")

    def clear_scope(self) -> None:
        """Drop all indexed content (pages/chunks/vectors, live AND staged)
        + the model marker. Job rows are kept for history. Used when a
        model change makes the existing vector space invalid."""
        with self.lock, _LockedRetry(self.db):
            for t in ("pages", "chunks", "chunks_fts", "vectors",
                      "sparse_vectors", "term_stats") + _STAGE_TABLES:
                self.db.execute(f"DELETE FROM {t}")  # nosec B608
            self.db.execute(
                "DELETE FROM meta WHERE key='embed_model' OR key LIKE 'seen:%'"
                " OR key LIKE 'levels:%' OR key LIKE 'warns:%'"
                " OR key LIKE 'keep:%' OR key LIKE 'build:%'")
            self._cache = None  # wiped content must not answer queries

    def set_model_id(self, model_id: str) -> None:
        with self.lock, _LockedRetry(self.db):
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)",
                            (model_id,))

    def _rebuild_term_stats(self) -> None:
        """Recompute per-term document frequency from the live sparse vectors.
        Called inside the publish transaction so BM25 idf is always in step
        with the snapshot. Must run inside a transaction on self.db."""
        n = 0
        term_chunks: list = []
        for (blob,) in self.db.execute("SELECT payload FROM sparse_vectors"):
            k = len(blob) // 12
            n += 1
            # df is documents-per-term — dedupe terms inside each doc,
            # then one vectorized unique+count over the corpus (#255)
            term_chunks.append(
                np.unique(np.frombuffer(blob[:8 * k], dtype=np.int64)))
        if term_chunks:
            uniq, cnts = np.unique(np.concatenate(term_chunks),
                                   return_counts=True)
            counts = dict(zip(uniq.tolist(), cnts.tolist()))
        else:
            counts = {}
        self.db.execute("DELETE FROM term_stats")
        self.db.executemany("INSERT INTO term_stats VALUES (?,?)",
                            sorted(counts.items()))
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('bm25_docs', ?)",
                        (str(n),))

    def term_stats(self) -> tuple[dict[int, int], int]:
        """(term → document frequency, docs-with-sparse-vectors) for BM25 idf.
        Empty stats mean the index predates stats support — callers fall back
        to unweighted tf scoring until the next publish rebuilds them."""
        with self._rlock:
            raw = self.meta_get("bm25_docs")
            df = {t: d for t, d in self._read().execute(
                "SELECT term_id, df FROM term_stats")}
        return df, int(raw) if raw else 0

    def snapshot_epoch(self) -> int:
        """Monotonic publish counter — evidence/citation handles compare
        against it to detect a snapshot that changed since they were taken."""
        raw = self.meta_get("snapshot_epoch")
        return int(raw) if raw else 0

    def publish(self, seen_page_ids: set[str],
                job_id: str = SCRATCH_JOB) -> dict:
        """Atomically replace the live snapshot: this job's staged pages plus
        the live pages listed in seen_page_ids (transient-failed pages carry
        their previous content forward). One transaction — readers see the
        old snapshot or the new one, never a mix. Pages absent from the set
        are confirmed removals and are DELETED.

        Returns {staged, deleted, kept} — surfaced in job state so a
        refresh's effect is auditable (#176)."""
        with self.lock, _LockedRetry(self.db):
            staged_n = deleted_n = 0
            self.db.execute(
                "CREATE TEMP TABLE IF NOT EXISTS pub_seen(pid TEXT PRIMARY KEY)")
            self.db.execute("DELETE FROM pub_seen")
            for batch in _batches(sorted(seen_page_ids)):
                self.db.executemany("INSERT INTO pub_seen VALUES (?)",
                                    [(p,) for p in batch])
            self.db.execute(
                "CREATE TEMP TABLE IF NOT EXISTS pub_staged(pid TEXT PRIMARY KEY)")
            self.db.execute("DELETE FROM pub_staged")
            self.db.execute(
                "INSERT INTO pub_staged SELECT DISTINCT page_id "
                "FROM pages_stage WHERE job_id=?", (job_id,))
            # doomed live rows: restaged (replaced below) or no longer seen
            doom = ("page_id IN (SELECT pid FROM pub_staged) "
                    "OR page_id NOT IN (SELECT pid FROM pub_seen)")
            doomed_chunks = ("SELECT chunk_id FROM chunks WHERE " + doom)  # nosec B608
            deleted_n = self.db.execute(
                "SELECT COUNT(*) FROM pages WHERE page_id NOT IN "
                "(SELECT pid FROM pub_seen)").fetchone()[0]
            staged_n = self.db.execute(
                "SELECT COUNT(*) FROM pub_staged").fetchone()[0]
            self.db.execute(
                f"DELETE FROM chunks_fts WHERE chunk_id IN ({doomed_chunks})")  # nosec B608
            self.db.execute(
                f"DELETE FROM vectors WHERE chunk_id IN ({doomed_chunks})")  # nosec B608
            self.db.execute(
                f"DELETE FROM sparse_vectors WHERE chunk_id IN ({doomed_chunks})")  # nosec B608
            self.db.execute(f"DELETE FROM chunks WHERE {doom}")  # nosec B608
            self.db.execute(f"DELETE FROM pages WHERE {doom}")  # nosec B608
            # staged content becomes the live snapshot
            self.db.execute(
                "INSERT INTO pages(page_id,parent_id,title,url,level,version,"
                "fetched_at,content_hash,warnings,active) SELECT page_id,"
                "parent_id,title,url,level,version,fetched_at,content_hash,"
                "warnings,1 FROM pages_stage WHERE job_id=?", (job_id,))
            self.db.execute(
                "INSERT INTO chunks(chunk_id,page_id,heading,kind,ordinal,"
                "text,active) SELECT chunk_id,page_id,heading,kind,ordinal,"
                "text,1 FROM chunks_stage WHERE job_id=?", (job_id,))
            self.db.execute(
                "INSERT INTO chunks_fts(chunk_id,text) SELECT chunk_id,text "
                "FROM chunks_fts_stage WHERE job_id=?", (job_id,))
            self.db.execute(
                "INSERT INTO vectors SELECT chunk_id,vec FROM vectors_stage "
                "WHERE job_id=?", (job_id,))
            self.db.execute(
                "INSERT INTO sparse_vectors SELECT chunk_id,payload "
                "FROM sparse_vectors_stage WHERE job_id=?", (job_id,))
            if self._ensure_ann():
                # mirror the vec0 ANN table alongside `vectors` (#251):
                # drop any row whose live vector no longer exists, then
                # upsert this job's staged blobs — both cheap anti-join /
                # upsert shapes, never a full rebuild.
                dim = self.db.execute(
                    "SELECT length(vec)/4 FROM vectors LIMIT 1"
                ).fetchone()
                if dim and dim[0]:
                    try:
                        self.db.execute(
                            f"CREATE VIRTUAL TABLE IF NOT EXISTS "
                            f"vec_chunks USING vec0(chunk_id TEXT "
                            f"PRIMARY KEY, embedding float[{dim[0]}] "
                            f"distance_metric=cosine)")
                    except sqlite3.OperationalError:
                        # dim changed under us (model swap) → rebuild
                        self.db.execute("DROP TABLE IF EXISTS vec_chunks")
                        self.db.execute(
                            f"CREATE VIRTUAL TABLE vec_chunks USING "
                            f"vec0(chunk_id TEXT PRIMARY KEY, embedding "
                            f"float[{dim[0]}] distance_metric=cosine)")
                    self.db.execute(
                        "DELETE FROM vec_chunks WHERE chunk_id NOT IN "
                        "(SELECT chunk_id FROM vectors)")
                    self.db.execute(
                        "INSERT INTO vec_chunks(chunk_id,embedding) "
                        "SELECT chunk_id,vec FROM vectors WHERE "
                        "chunk_id NOT IN (SELECT chunk_id FROM "
                        "vec_chunks)")
            for t in _STAGE_TABLES:
                self.db.execute(f"DELETE FROM {t} WHERE job_id=?", (job_id,))  # nosec B608
            self._purge_dead_stage()
            self._rebuild_term_stats()
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('snapshot_epoch', ?)",
                (str(self.snapshot_epoch() + 1),))
        kept = max(0, len(seen_page_ids) - staged_n)
        self._warm_search_cache()
        return {"staged": staged_n, "deleted": deleted_n, "kept": kept}

    def _warm_search_cache(self) -> None:
        """Pre-build the retrieval structures off the request path — the
        first search after a publish used to pay the full postings scan
        (~1s at 50k chunks) inside the user's query (#255). Epoch-guarded:
        a publish landing mid-build is caught by search_cache()'s swap
        check, so a stale build can never displace a fresher snapshot."""
        def _run() -> None:
            try:
                self.search_cache()
            except Exception:      # noqa: BLE001,S110 — opportunistic warm only
                pass
            finally:
                with self._rlock:
                    self._warm_threads.discard(
                        threading.current_thread())
        t = threading.Thread(target=_run, name="cache-warm", daemon=True)
        with self._rlock:
            self._warm_threads.add(t)
        t.start()

    # ---- query ----
    def active_pages(self) -> dict[str, dict]:
        with self._rlock:
            return {r[0]: {"title": r[1], "url": r[2], "level": r[3],
                           "version": r[4], "fetched_at": r[5]}
                    for r in self._read().execute(
                        "SELECT page_id,title,url,level,version,fetched_at "
                        "FROM pages WHERE active=1")}

    def active_chunks(self) -> list[dict]:
        with self._rlock:
            return [{"chunk_id": r[0], "page_id": r[1], "heading": r[2],
                     "kind": r[3], "ordinal": r[4], "text": r[5]}
                    for r in self._read().execute(
                        "SELECT chunk_id,page_id,heading,kind,ordinal,text "
                        "FROM chunks WHERE active=1")]

    def chunks_by_ids(self, chunk_ids: list[str]) -> list[dict]:
        """Full chunk rows for specific ids, in requested order."""
        if not chunk_ids:
            return []
        out: dict[str, dict] = {}
        with self._rlock:
            step = self._var_batch()
            for off in range(0, len(chunk_ids), step):
                part = chunk_ids[off:off + step]
                q = ",".join("?" * len(part))
                for r in self._read().execute(
                        f"SELECT chunk_id,page_id,heading,kind,ordinal,text "  # nosec B608
                        f"FROM chunks WHERE chunk_id IN ({q})", part):
                    out[r[0]] = {"chunk_id": r[0], "page_id": r[1],
                                 "heading": r[2], "kind": r[3],
                                 "ordinal": r[4], "text": r[5]}
        return [out[c] for c in chunk_ids if c in out]

    def page_chunks(self, page_id: str) -> list[dict]:
        """A page's active chunks in ordinal order."""
        with self._rlock:
            return [{"chunk_id": r[0], "page_id": r[1], "heading": r[2],
                     "kind": r[3], "ordinal": r[4], "text": r[5]}
                    for r in self._read().execute(
                        "SELECT chunk_id,page_id,heading,kind,ordinal,text "
                        "FROM chunks WHERE page_id=? AND active=1 "
                        "ORDER BY ordinal", (page_id,))]

    def live_page_hash(self, page_id: str) -> str | None:
        """content_hash of the page's live row — a re-crawl with the same
        hash can reuse the page's rows instead of re-embedding."""
        with self._rlock:
            r = self._read().execute(
                "SELECT content_hash FROM pages WHERE page_id=? AND active=1",
                (page_id,)).fetchone()
        return r[0] if r else None

    def live_page_version(self, page_id: str) -> str | None:
        """version string of the page's live row — SharePoint items skip
        the download entirely when lastModifiedDateTime is unchanged."""
        with self.lock:
            r = self.db.execute(
                "SELECT version FROM pages WHERE page_id=? AND active=1",
                (page_id,)).fetchone()
        return r[0] if r else None

    def copy_live_to_stage(self, page_id: str, job_id: str) -> bool:
        """Copy the page's live chunk/vector/sparse/FTS rows into this
        job's staging — for re-crawls where content_hash matched (the
        page is byte-identical, so its embeddings still hold). Clears any
        same-job staged rows first. False when no live page exists."""
        with self.lock, _LockedRetry(self.db):
            page = self.db.execute(
                "SELECT page_id,parent_id,title,url,level,version,"
                "fetched_at,content_hash,warnings FROM pages "
                "WHERE page_id=? AND active=1", (page_id,)).fetchone()
            if page is None:
                return False
            # refresh fetched_at so staleness warnings don't accuse an
            # unchanged page — content_hash stays the live (verified) value
            self.db.execute(
                "INSERT OR REPLACE INTO pages_stage "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, page[0], page[1], page[2], page[3], page[4],
                 page[5], time.time(), page[7], page[8]))
            old = [r[0] for r in self.db.execute(
                "SELECT chunk_id FROM chunks_stage "
                "WHERE job_id=? AND page_id=?", (job_id, page_id))]
            for cid in old:
                self.db.execute(
                    "DELETE FROM chunks_fts_stage "
                    "WHERE job_id=? AND chunk_id=?", (job_id, cid))
                self.db.execute(
                    "DELETE FROM vectors_stage "
                    "WHERE job_id=? AND chunk_id=?", (job_id, cid))
                self.db.execute(
                    "DELETE FROM sparse_vectors_stage "
                    "WHERE job_id=? AND chunk_id=?", (job_id, cid))
            self.db.execute(
                "DELETE FROM chunks_stage WHERE job_id=? AND page_id=?",
                (job_id, page_id))
            rows = self.db.execute(
                "SELECT chunk_id,heading,kind,ordinal,text FROM chunks "
                "WHERE page_id=? AND active=1 ORDER BY ordinal",
                (page_id,)).fetchall()
            for cid, heading, kind, ordinal, text in rows:
                self.db.execute(
                    "INSERT INTO chunks_stage VALUES (?,?,?,?,?,?,?)",
                    (job_id, cid, page_id, heading, kind, ordinal, text))
                self.db.execute(
                    "INSERT INTO chunks_fts_stage VALUES (?,?,?)",
                    (job_id, cid, text))
                v = self.db.execute(
                    "SELECT vec FROM vectors WHERE chunk_id=?",
                    (cid,)).fetchone()
                if v:
                    self.db.execute(
                        "INSERT INTO vectors_stage VALUES (?,?,?)",
                        (job_id, cid, v[0]))
                s = self.db.execute(
                    "SELECT payload FROM sparse_vectors WHERE chunk_id=?",
                    (cid,)).fetchone()
                if s:
                    self.db.execute(
                        "INSERT INTO sparse_vectors_stage VALUES (?,?,?)",
                        (job_id, cid, s[0]))
            return True

    def search_cache(self) -> _SearchCache:
        """Retrieval structures for the current snapshot, rebuilt only
        when the epoch advanced. The build reads through the WAL read
        connection OFF the writer lock (#188) — at 10k+ pages the scan
        is seconds-scale and must not stall status/crawl_step. A publish
        landing mid-build bumps the epoch; the swap keeps the fresher
        cache and the next call rebuilds. Concurrent callers share one
        build — the first becomes the builder and the rest wait for its
        install, so a racing equal-epoch build can never displace the
        snapshot another caller already received (#255)."""
        epoch = self.snapshot_epoch()
        with self.lock:
            while True:
                if self._cache is not None and self._cache.epoch == epoch:
                    return self._cache
                if not self._building_cache:
                    self._building_cache = True
                    break
                self._cache_cv.wait()
        try:
            c = self._build_search_cache(epoch)
        except Exception:
            with self.lock:
                self._building_cache = False
                self._cache_cv.notify_all()
            raise
        with self.lock:
            if self._cache is None or self._cache.epoch < epoch:
                self._cache = c
            self._building_cache = False
            self._cache_cv.notify_all()
            return self._cache

    def drop_search_cache(self) -> None:
        with self.lock:
            self._cache = None

    def _build_search_cache(self, epoch: int) -> _SearchCache:
        c = _SearchCache(epoch)
        rows = self._read().execute(
            "SELECT chunk_id,page_id FROM chunks WHERE active=1 "
            "ORDER BY rowid").fetchall()
        c.ids = [r[0] for r in rows]
        c.ids_set = set(c.ids)
        c.row = {cid: i for i, cid in enumerate(c.ids)}
        c.chunk_page = dict(rows)
        # dense: float16 halves RSS vs float32; search-time matmul runs
        # blocked in float32. Mismatched-dim blobs (model swap leftovers)
        # stay zero → score 0, mirroring _cosine's skip. Above the
        # whole-scope cap the ANN leg (vec_chunks) serves dense instead —
        # building a few-hundred-MB matrix for an index that can't be
        # scanned is pure waste (#251).
        vecs = ({} if len(c.ids) > _ANN_SCOPE_MIN
                else self.vectors_for(c.ids))
        if vecs:
            dim = next(iter(vecs.values())).shape[0]
            dense = np.zeros((len(c.ids), dim), dtype=np.float32)
            for cid, v in vecs.items():
                if v.shape[0] == dim:
                    dense[c.row[cid]] = v
            n = np.linalg.norm(dense, axis=1, keepdims=True)
            dense = dense / (n + 1e-9)
            c.dense = dense.astype(np.float16)
        # inverted sparse index: term → (row indices, weights). One argsort
        # over a single concatenated postings array replaces the per-term
        # Python get/append loop — ~8.9M appends → a few vector ops at 50k
        # chunks (#255)
        term_chunks: list = []
        row_chunks: list = []
        wt_chunks: list = []
        for cid, blob in self._read().execute(
                "SELECT s.chunk_id,s.payload FROM sparse_vectors s "
                "JOIN chunks ch ON ch.chunk_id=s.chunk_id "
                "WHERE ch.active=1"):
            i = c.row.get(cid)
            if i is None:
                # the two read-txns above can straddle a publish — a
                # chunk from the newer epoch isn't in c.row; the epoch
                # swap check rebuilds instead of serving it torn
                continue
            k = len(blob) // 12
            term_chunks.append(np.frombuffer(blob[:8 * k], dtype=np.int64))
            row_chunks.append(np.full(k, i, dtype=np.int32))
            wt_chunks.append(np.frombuffer(blob[8 * k:], dtype=np.float32))
        if term_chunks:
            terms = np.concatenate(term_chunks)
            rows = np.concatenate(row_chunks)
            wts = np.concatenate(wt_chunks)
            # stable: blobs arrive in chunk order, so per-term row lists
            # stay ascending like the old append loop produced
            order = np.argsort(terms, kind="stable")
            terms, rows, wts = terms[order], rows[order], wts[order]
            # sorted → term boundaries are just diffs; np.unique would pay
            # a second full sort
            first = np.concatenate(
                ([0], np.flatnonzero(np.diff(terms)) + 1))
            uniq = terms[first]
            bounds = np.append(first[1:], len(terms))
            c.sparse_idx = {
                t: (rows[s:e], wts[s:e])
                for t, s, e in zip(uniq.tolist(), first.tolist(),
                                   bounds.tolist())}
        c.df, c.n_docs = self.term_stats()
        c.pages = self.active_pages()
        return c

    def fts_search(self, query: str, limit: int = 30,
                   active: set[str] | None = None) -> list[tuple[str, float]]:
        with self._rlock:
            try:
                rows = self._read().execute(
                    "SELECT chunk_id, rank FROM chunks_fts WHERE chunks_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (" OR ".join('"' + w.replace('"', '""') + '"'
                                 for w in query.split()[:16] if w.strip()) or '""',
                     limit)).fetchall()
            except sqlite3.OperationalError:
                return []
        if active is None:
            active = {c["chunk_id"] for c in self.active_chunks()}
        return [(r[0], -r[1]) for r in rows if r[0] in active]

    def vectors_for(self, chunk_ids: list[str]) -> dict[str, np.ndarray]:
        if not chunk_ids:
            return {}
        out = {}
        with self._rlock:
            step = self._var_batch()
            for off in range(0, len(chunk_ids), step):
                part = chunk_ids[off:off + step]
                q = ",".join("?" * len(part))
                for cid, b in self._read().execute(
                        f"SELECT chunk_id,vec FROM vectors "  # nosec B608
                        f"WHERE chunk_id IN ({q})", part):
                    out[cid] = np.frombuffer(b, dtype=np.float32)
        return out

    # ---- ANN (sqlite-vec) ------------------------------------------------
    # vec0 `vec_chunks` mirrors `vectors` so dense top-k is an index scan,
    # not a RAM-resident matmul — that's what lifts the whole-scope cap
    # for >100k-chunk indexes (#251). Degrades silently: no extension →
    # the O(n) scan path stays.

    def _ann_load(self, conn: sqlite3.Connection) -> bool:
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            return True
        except Exception:               # noqa: BLE001 — ext absent or
            return False                # unloadable → scan fallback
        finally:
            # leave extension loading OFF — the crawl worker's conn must
            # not stay wider than it needs to
            try:
                conn.enable_load_extension(False)
            except Exception:           # noqa: BLE001,S110 — best-effort
                pass

    def _ensure_ann(self) -> bool:
        """vec_chunks exists + backfilled. Runs once per Index; failure
        latches False so no path keeps paying the probe."""
        if self._ann is not None:
            return self._ann
        with self.lock:
            if self._ann is not None:
                return self._ann
            try:
                # read conn first — a ready vec_chunks answers KNN on
                # mode=ro (proxy/daemon) without any write. _read() can
                # raise transient SetupError (WAL pragma mid-checkpoint):
                # ANY failure here latches False — ANN must never be
                # able to break a crawl or a query.
                conn = self._read() if not self._readonly else self.db
                if not self._ann_load(conn):
                    self._ann = False
                    return False
                exists = conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE "
                    "name='vec_chunks'").fetchone()[0]
                if exists:
                    self._ann = True
                    return True
                if self._readonly:
                    # table absent and we can't create — scan path
                    self._ann = False
                    return False
                if not self._ann_load(self.db):
                    self._ann = False
                    return False
                row = self.db.execute(
                    "SELECT vec FROM vectors LIMIT 1").fetchone()
                if row is None:
                    self._ann = True          # empty — lazy at publish
                    return True
                dim = len(row[0]) // 4
                self.db.execute(
                    f"CREATE VIRTUAL TABLE vec_chunks USING vec0("
                    f"chunk_id TEXT PRIMARY KEY, embedding float[{dim}] "
                    f"distance_metric=cosine)")
                self.db.execute(
                    "INSERT INTO vec_chunks(chunk_id,embedding) "
                    "SELECT chunk_id,vec FROM vectors")
                self.db.commit()
                self._ann = True
            except Exception:           # noqa: BLE001 — any create/backfill
                self._ann = False       # failure → scan fallback
        return self._ann

    def ann_available(self) -> bool:
        return self._ensure_ann()

    def ann_topk(self, qvec: np.ndarray, k: int,
                 allowed: set[str] | None = None) -> list[tuple[str, float]]:
        """K nearest live chunks by cosine → [(chunk_id, score)]. The
        subtree case over-fetches then intersects — a vec0 KNN can't
        take an arbitrary IN-list, so the filter lands in Python."""
        if not self._ensure_ann():
            return []
        try:
            conn = self._read()
        except Exception:               # noqa: BLE001 — transient WAL
            return []                   # pragma race → caller's legs
        if not self._ann_load(conn):    # still answer via sparse/fts
            self._ann = False
            return []
        fetch = k if allowed is None else min(len(allowed), k * 10)
        try:
            rows = conn.execute(
                "SELECT chunk_id, distance FROM vec_chunks "
                "WHERE embedding MATCH ? AND k = ?",
                (qvec.astype(np.float32).tobytes(), fetch)).fetchall()
        except sqlite3.DatabaseError:
            # any read-side sqlite failure (OperationalError and kin —
            # torn publish/close races surface as plain DatabaseError) →
            # the sparse/FTS legs still answer (#255)
            return []
        out = []
        for cid, dist in rows:
            if allowed is not None and cid not in allowed:
                continue
            out.append((cid, 1.0 - float(dist)))
            if len(out) >= k:
                break
        return out

    def quick_integrity(self) -> bool:
        """PRAGMA quick_check — same corruption catch as integrity_check
        but skips the expensive FK/index consistency walk. Suitable for
        per-call health surfaces (status) where a full scan is too slow.
        Read path — safe on the WAL read connection (#199)."""
        try:
            row = self._read().execute("PRAGMA quick_check").fetchone()
            return bool(row and row[0] == "ok")
        except sqlite3.DatabaseError:
            return False

    def integrity(self) -> bool:
        """PRAGMA integrity_check — False means the DB file is damaged and
        should be deleted + rebuilt (never auto-repaired here)."""
        try:
            with self.lock:
                row = self.db.execute("PRAGMA integrity_check").fetchone()
            return bool(row and row[0] == "ok")
        except sqlite3.DatabaseError:
            return False

    def stats(self) -> dict:
        with self._rlock:
            r = self._read()
            p = r.execute("SELECT COUNT(*) FROM pages WHERE active=1").fetchone()[0]
            c = r.execute("SELECT COUNT(*) FROM chunks WHERE active=1").fetchone()[0]
            snap = r.execute(
                "SELECT MAX(fetched_at) FROM pages WHERE active=1").fetchone()[0]
            return {"pages": p, "chunks": c, "as_of": snap}

    # ---- #240 descendent-scope reuse ---------------------------------

    def has_page(self, page_id: str) -> bool:
        """True when page_id is in the LIVE snapshot."""
        with self._rlock:
            return self._read().execute(
                "SELECT 1 FROM pages WHERE page_id=? AND active=1",
                (page_id,)).fetchone() is not None

    def subtree_pages(self, root_id: str) -> set[str]:
        """root_id plus every live page whose parent chain reaches it.
        parent_id is maintained by the crawler's BFS — a page outside
        the subtree never names root_id as an ancestor."""
        with self._rlock:
            rows = self._read().execute(
                "SELECT page_id,parent_id FROM pages WHERE active=1"
            ).fetchall()
        parent = {pid: par for pid, par in rows}
        out = {root_id} if root_id in parent else set()
        for pid in parent:
            cur = pid
            seen: set[str] = set()
            while cur in parent and cur not in seen:
                seen.add(cur)
                if parent[cur] == root_id:
                    out.add(pid)
                    break
                cur = parent[cur]
        return out

    def subtree_pages_prefix(self, rel_prefix: str) -> set[str]:
        """Local-knowledge variant: pages are keyed by path relative to
        the scope root — a descendent folder's subtree is a prefix set."""
        with self._rlock:
            rows = self._read().execute(
                "SELECT page_id FROM pages WHERE active=1 AND "
                "(page_id=? OR page_id LIKE ?)",
                (rel_prefix, rel_prefix.rstrip("/") + "/%")).fetchall()
        return {r[0] for r in rows}

    def subtree_chunk_ids(self, page_ids: set[str]) -> set[str]:
        """Active chunk_ids belonging to the given pages — the
        `allow_chunks` filter a descendent scope applies over its
        ancestor's snapshot."""
        cache = self.search_cache()
        return {cid for cid, pid in cache.chunk_page.items()
                if pid in page_ids}

    def subtree_pages_entry(self, root_entry: str) -> set[str]:
        """SharePoint variant (#240): SP folders are queue entries, not
        page rows — the crawler persists the entry→parent map into
        `entry_parents` meta at publish. The subtree = all live pages
        whose entry is a descendant of root_entry (plus the entry itself
        when it is a live page)."""
        raw = self.meta_get("entry_parents")
        if not raw:
            return set()
        parents: dict[str, str] = json.loads(raw)
        kids: dict[str, set[str]] = {}
        for child, par in parents.items():
            kids.setdefault(par, set()).add(child)
        live = set(self.active_pages())
        out: set[str] = set()
        stack = [root_entry]
        while stack:
            for c in kids.get(stack.pop(), ()):
                if c not in out:
                    out.add(c)
                    stack.append(c)
        # an item: entry's page row is file:{drive}:{id}; page: entries
        # are already page ids — translate before intersecting live
        def _page_form(entry: str) -> str:
            return ("file:" + entry[5:]
                    if entry.startswith("item:") else entry)
        return {_page_form(e) for e in (out | {root_entry})} & live

    def import_subtree(self, src: Index, page_ids: set[str]) -> int:
        """Copy the listed pages + their chunk/vector/sparse/FTS rows from
        `src`'s live snapshot into THIS index's live tables — the seed
        for a descendent scope materializing out of an ancestor. Rows
        carry content_hash so the following crawl's hash-match reuses
        embeddings instead of recomputing them. One transaction; bumps
        this index's epoch. Returns pages imported."""
        if not page_ids:
            return 0
        ids = sorted(page_ids)
        with self.lock, _LockedRetry(self.db):
            pages = []
            for batch in _batches(ids):
                pages += src._read().execute(
                    "SELECT page_id,parent_id,title,url,level,version,"
                    "fetched_at,content_hash,warnings FROM pages "
                    f"WHERE active=1 AND page_id IN "
                    f"({','.join('?' * len(batch))})",
                    batch).fetchall()
            self.db.executemany(
                "INSERT OR REPLACE INTO pages(page_id,parent_id,title,url,"
                "level,version,fetched_at,content_hash,warnings,active) "
                "VALUES (?,?,?,?,?,?,?,?,?,1)", pages)
            chunks = []
            for batch in _batches(ids):
                chunks += src._read().execute(
                    "SELECT chunk_id,page_id,heading,kind,ordinal,text "
                    "FROM chunks WHERE active=1 AND page_id IN "
                    f"({','.join('?' * len(batch))})",
                    batch).fetchall()
            chunk_ids = [c[0] for c in chunks]
            for batch in _batches(chunk_ids):
                self.db.executemany(
                    "DELETE FROM chunks_fts WHERE chunk_id=?",
                    [(c,) for c in batch])
                self.db.execute(
                    f"DELETE FROM chunks WHERE chunk_id IN "
                    f"({','.join('?' * len(batch))})", batch)
            self.db.executemany(
                "INSERT INTO chunks(chunk_id,page_id,heading,kind,ordinal,"
                "text,active) VALUES (?,?,?,?,?,?,1)", chunks)
            fts_rows, vecs, sparse = [], [], []
            for batch in _batches(chunk_ids):
                ph = ",".join("?" * len(batch))
                fts_rows += src._read().execute(
                    "SELECT chunk_id,text FROM chunks_fts "
                    f"WHERE chunk_id IN ({ph})", batch).fetchall()
                vecs += src._read().execute(
                    "SELECT chunk_id,vec FROM vectors "
                    f"WHERE chunk_id IN ({ph})", batch).fetchall()
                sparse += src._read().execute(
                    "SELECT chunk_id,payload FROM sparse_vectors "
                    f"WHERE chunk_id IN ({ph})", batch).fetchall()
            self.db.executemany(
                "INSERT INTO chunks_fts(chunk_id,text) VALUES (?,?)",
                fts_rows)
            for batch in _batches(chunk_ids):
                ph = ",".join("?" * len(batch))
                self.db.execute(
                    f"DELETE FROM vectors WHERE chunk_id IN ({ph})", batch)
                self.db.execute(
                    f"DELETE FROM sparse_vectors WHERE chunk_id IN ({ph})",
                    batch)
            self.db.executemany(
                "INSERT INTO vectors VALUES (?,?)", vecs)
            self.db.executemany(
                "INSERT INTO sparse_vectors VALUES (?,?)", sparse)
            self._rebuild_term_stats()
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('snapshot_epoch', ?)",
                (str(self.snapshot_epoch() + 1),))
            return len(pages)


def find_covering(index_root: Path, exclude_key: str, covers,
                  principal: str | None = None) -> dict | None:
    """#240: find the freshest fully-crawled sibling index that covers the
    requested root — the ancestor scope a descendent can read through.

    index_root: the directory holding per-scope `<key>.db` files.
    covers(idx): membership check — returns the subtree size when the
    candidate's snapshot contains the root, else 0/None.
    principal: when set, only indexes stamped with the same `principal`
    meta are eligible — a different account may not see these pages.

    Returns {key, pages, as_of} of the freshest covering ancestor."""
    best = None
    for p in index_root.glob("*.db"):
        key = p.stem
        if key == exclude_key:
            continue
        try:
            src = Index(p, readonly=True)
            job = src.latest_job()
            if not job or job["state"] != "done":
                continue                        # partial coverage only
            if principal is not None and \
                    src.meta_get("principal") != principal:
                continue                        # different account's view
            n = covers(src)
            if not n:
                continue
            as_of = src.stats().get("as_of") or 0
            if best is None or as_of > best["as_of"]:
                best = {"key": key, "pages": n, "as_of": as_of}
        except Exception:                       # noqa: BLE001,S112 —
            continue                    # unreadable sibling / mid-write race
    return best
