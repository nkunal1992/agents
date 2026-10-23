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
    __slots__ = ("dense", "df", "epoch", "ids", "ids_set", "n_docs",
                 "pages", "row", "sparse_idx")

    def __init__(self, epoch):
        self.epoch = epoch
        self.ids: list[str] = []
        self.ids_set: set[str] = set()
        self.row: dict[str, int] = {}
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
    _TRIES = 6
    _BACKOFF = (0.05, 0.1, 0.2, 0.4, 0.8)

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
        # read-only shadow connection: WAL lets a second reader see the
        # last-committed snapshot without touching the writer's
        # transaction — light tools (status/crawl_step/evidence) must
        # never queue behind a publish sweep or a cache rebuild (#188)
        self._rdb: sqlite3.Connection | None = None
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
                f"index unreadable at {path} — delete it and re-crawl "
                "(or run `clean` to drop every index)") from e
        self.lock = threading.RLock()
        self._cache: _SearchCache | None = None
        if readonly:
            self._rdb = self.db        # the main conn already is ro
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
        """Release both connections — Windows refuses file deletion while
        the read conn is still open (e2e cleanup hit WinError 32)."""
        for conn in (self._rdb, self.db):
            if conn is not None:
                try:
                    conn.close()
                except Exception:           # noqa: BLE001,S110 — close
                    pass                     # is best-effort at teardown
        self._rdb = None

    def _read(self) -> sqlite3.Connection:
        """Read-only connection for pure queries — WAL snapshot semantics
        mean a mid-publish read simply sees the previous snapshot."""
        if self._rdb is None:
            uri = f"file:{self._path}?mode=ro"
            self._rdb = sqlite3.connect(uri, uri=True, timeout=5,
                                        check_same_thread=False)
            # assert once, on the READ conn — touching self.db here would
            # corrupt an in-flight write transaction (readers race it)
            mode = self._rdb.execute("PRAGMA journal_mode").fetchone()[0]
            if mode != "wal":
                # read/write deadlock risk — WAL is load-bearing (#188)
                from .settings import SetupError
                raise SetupError(
                    f"index journal_mode is {mode!r}, expected wal")
        return self._rdb

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
                        owner_host = row[0].rsplit(":", 2)[0]
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
        counts: dict[int, int] = {}
        n = 0
        for (blob,) in self.db.execute("SELECT payload FROM sparse_vectors"):
            k = len(blob) // 12
            n += 1
            for t in np.unique(np.frombuffer(blob[:8 * k], dtype=np.int64)):
                counts[int(t)] = counts.get(int(t), 0) + 1
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
            for t in _STAGE_TABLES:
                self.db.execute(f"DELETE FROM {t} WHERE job_id=?", (job_id,))  # nosec B608
            self._purge_dead_stage()
            self._rebuild_term_stats()
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('snapshot_epoch', ?)",
                (str(self.snapshot_epoch() + 1),))
        kept = max(0, len(seen_page_ids) - staged_n)
        return {"staged": staged_n, "deleted": deleted_n, "kept": kept}

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
        cache and the next call rebuilds."""
        epoch = self.snapshot_epoch()
        with self.lock:
            if self._cache is not None and self._cache.epoch == epoch:
                return self._cache
        c = self._build_search_cache(epoch)
        with self.lock:
            if self._cache is None or self._cache.epoch <= epoch:
                self._cache = c
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
        # dense: float16 halves RSS vs float32; search-time matmul runs
        # blocked in float32. Mismatched-dim blobs (model swap leftovers)
        # stay zero → score 0, mirroring _cosine's skip.
        vecs = self.vectors_for(c.ids)
        if vecs:
            dim = next(iter(vecs.values())).shape[0]
            dense = np.zeros((len(c.ids), dim), dtype=np.float32)
            for cid, v in vecs.items():
                if v.shape[0] == dim:
                    dense[c.row[cid]] = v
            n = np.linalg.norm(dense, axis=1, keepdims=True)
            dense = dense / (n + 1e-9)
            c.dense = dense.astype(np.float16)
        # inverted sparse index: term → (row indices, weights)
        postings: dict[int, tuple[list[int], list[float]]] = {}
        for cid, blob in self._read().execute(
                "SELECT s.chunk_id,s.payload FROM sparse_vectors s "
                "JOIN chunks ch ON ch.chunk_id=s.chunk_id "
                "WHERE ch.active=1"):
            i = c.row[cid]
            k = len(blob) // 12
            idx_arr = np.frombuffer(blob[:8 * k], dtype=np.int64)
            val_arr = np.frombuffer(blob[8 * k:], dtype=np.float32)
            for t, w in zip(idx_arr.tolist(), val_arr.tolist()):
                ent = postings.get(t)
                if ent is None:
                    postings[t] = ([i], [w])
                else:
                    ent[0].append(i)
                    ent[1].append(w)
        c.sparse_idx = {t: (np.asarray(rs, dtype=np.int32),
                            np.asarray(ws, dtype=np.float32))
                        for t, (rs, ws) in postings.items()}
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
