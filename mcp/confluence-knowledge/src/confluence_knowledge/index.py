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
"""

_STAGE_TABLES = ("pages_stage", "chunks_stage", "vectors_stage",
                 "sparse_vectors_stage", "chunks_fts_stage")
# writes that bypass the job system (tests, tools) use this stage owner
SCRATCH_JOB = ""


def _batches(ids: list[str], size: int = 5000):
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


class Index:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # timeout: retry transient locks (AV scan, second process, WAL
        # checkpoint) instead of failing instantly; check_same_thread: the
        # crawl worker and tool-call threads share this one connection —
        # `self.lock` serializes every access so writes never interleave
        try:
            self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
            self.db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
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
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),))
            # an uncommitted INSERT holds the write lock for the connection's
            # lifetime and deadlocks every other connection (#68)
            self.db.commit()

    # ---- jobs ----
    def create_job(self, job_id: str, root_page: str) -> None:
        with self.lock, self.db:
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

    def abandon_running_jobs(self) -> None:
        """A new crawl supersedes any unfinished one (e.g. an earlier failed
        attempt) so it can't leave coverage reported as partial forever."""
        with self.lock:
            self.db.execute("UPDATE jobs SET state='abandoned' WHERE state='running'")
            self.db.commit()

    def latest_job(self) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT job_id,state,errors FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        return row and {"job_id": row[0], "state": row[1], "errors": json.loads(row[2])}

    def save_job(self, job_id: str, state: str, queue: list, errors: list) -> None:
        with self.lock:
            self.db.execute(
                "UPDATE jobs SET state=?, queue=?, errors=? WHERE job_id=?",
                (state, json.dumps(queue), json.dumps(errors), job_id))
            self.db.commit()

    def meta_get(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute(
                "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def job_progress(self, job_id: str) -> dict:
        """Snapshot the agent can poll while a background worker crawls."""
        job = self.job(job_id)
        if job is None:
            raise KeyError(job_id)
        seen = self.meta_get(f"seen:{job_id}")
        return {"job_id": job_id, "state": job["state"],
                "remaining": len(job["queue"]),
                "pages_seen": len(json.loads(seen)) if seen else 0,
                "errors": job["errors"]}

    def _purge_dead_stage(self) -> None:
        """Drop staging rows of jobs that can never publish (abandoned/done/
        error). Must run inside a transaction on self.db."""
        for t in _STAGE_TABLES:
            self.db.execute(
                f"DELETE FROM {t} WHERE job_id != ? AND job_id NOT IN "
                "(SELECT job_id FROM jobs WHERE state IN ('queued','running'))",
                (SCRATCH_JOB,))

    # ---- content ----
    def upsert_page(self, page: dict, chunks: list, vecs: np.ndarray | None,
                    sparse: list[dict[int, float]] | None = None,
                    job_id: str = SCRATCH_JOB) -> None:
        """Stage a crawled page under its job. Live tables are NEVER touched
        here — readers keep the last published snapshot until publish()."""
        with self.lock, self.db:
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

    def sparse_for(self, chunk_ids: list[str]) -> dict[str, dict[int, float]]:
        if not chunk_ids:
            return {}
        q = ",".join("?" * len(chunk_ids))
        out = {}
        with self.lock:
            for cid, blob in self.db.execute(
                    f"SELECT chunk_id,payload FROM sparse_vectors "
                    f"WHERE chunk_id IN ({q})", chunk_ids):
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
        with self.lock:
            for t in ("pages", "chunks", "chunks_fts", "vectors",
                      "sparse_vectors") + _STAGE_TABLES:
                self.db.execute(f"DELETE FROM {t}")
            self.db.execute(
                "DELETE FROM meta WHERE key='embed_model' OR key LIKE 'seen:%'"
                " OR key LIKE 'levels:%' OR key LIKE 'warns:%'"
                " OR key LIKE 'keep:%'")
            self.db.commit()

    def set_model_id(self, model_id: str) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)",
                            (model_id,))

    def snapshot_epoch(self) -> int:
        """Monotonic publish counter — evidence/citation handles compare
        against it to detect a snapshot that changed since they were taken."""
        raw = self.meta_get("snapshot_epoch")
        return int(raw) if raw else 0

    def publish(self, seen_page_ids: set[str], job_id: str = SCRATCH_JOB) -> None:
        """Atomically replace the live snapshot: this job's staged pages plus
        the live pages listed in seen_page_ids (transient-failed pages carry
        their previous content forward). One transaction — readers see the
        old snapshot or the new one, never a mix. Pages absent from the set
        are confirmed removals and are DELETED."""
        with self.lock, self.db:
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
            doomed_chunks = ("SELECT chunk_id FROM chunks WHERE " + doom)
            self.db.execute(
                f"DELETE FROM chunks_fts WHERE chunk_id IN ({doomed_chunks})")
            self.db.execute(
                f"DELETE FROM vectors WHERE chunk_id IN ({doomed_chunks})")
            self.db.execute(
                f"DELETE FROM sparse_vectors WHERE chunk_id IN ({doomed_chunks})")
            self.db.execute(f"DELETE FROM chunks WHERE {doom}")
            self.db.execute(f"DELETE FROM pages WHERE {doom}")
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
                self.db.execute(f"DELETE FROM {t} WHERE job_id=?", (job_id,))
            self._purge_dead_stage()
            self.db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('snapshot_epoch', ?)",
                (str(self.snapshot_epoch() + 1),))

    # ---- query ----
    def active_pages(self) -> dict[str, dict]:
        with self.lock:
            return {r[0]: {"title": r[1], "url": r[2], "level": r[3]}
                    for r in self.db.execute(
                        "SELECT page_id,title,url,level FROM pages WHERE active=1")}

    def active_chunks(self) -> list[dict]:
        with self.lock:
            return [{"chunk_id": r[0], "page_id": r[1], "heading": r[2],
                     "kind": r[3], "text": r[4]}
                    for r in self.db.execute(
                        "SELECT chunk_id,page_id,heading,kind,text FROM chunks WHERE active=1")]

    def fts_search(self, query: str, limit: int = 30,
                   active: set[str] | None = None) -> list[tuple[str, float]]:
        with self.lock:
            try:
                rows = self.db.execute(
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
        q = ",".join("?" * len(chunk_ids))
        with self.lock:
            return {cid: np.frombuffer(b, dtype=np.float32)
                    for cid, b in self.db.execute(
                        f"SELECT chunk_id,vec FROM vectors WHERE chunk_id IN ({q})",
                        chunk_ids)}

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
        with self.lock:
            p = self.db.execute("SELECT COUNT(*) FROM pages WHERE active=1").fetchone()[0]
            c = self.db.execute("SELECT COUNT(*) FROM chunks WHERE active=1").fetchone()[0]
            snap = self.db.execute(
                "SELECT MAX(fetched_at) FROM pages WHERE active=1").fetchone()[0]
            return {"pages": p, "chunks": c, "as_of": snap}
