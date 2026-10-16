"""Local persistent index: SQLite metadata + FTS5 keyword + vector blobs.

One DB per scope (site+account+root). Single-writer transactions; a completed
crawl is published atomically so a failed refresh never clobbers a good index.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import numpy as np

SCHEMA_VERSION = 1

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
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED, text);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, root_page TEXT, state TEXT, created_at REAL,
  queue TEXT, errors TEXT);
"""


class Index:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # timeout: retry transient locks (AV scan, second process, WAL
        # checkpoint) instead of failing instantly; check_same_thread: MCP
        # tools may run on worker threads
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),))
        # an uncommitted INSERT holds the write lock for the connection's
        # lifetime and deadlocks every other connection (#68)
        self.db.commit()

    # ---- jobs ----
    def create_job(self, job_id: str, root_page: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?)",
            (job_id, root_page, "queued", time.time(), json.dumps([root_page]), "[]"))
        self.db.commit()

    def job(self, job_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT root_page,state,queue,errors FROM jobs WHERE job_id=?",
            (job_id,)).fetchone()
        if not row:
            return None
        return {"root_page": row[0], "state": row[1],
                "queue": json.loads(row[2]), "errors": json.loads(row[3])}

    def save_job(self, job_id: str, state: str, queue: list, errors: list) -> None:
        self.db.execute(
            "UPDATE jobs SET state=?, queue=?, errors=? WHERE job_id=?",
            (state, json.dumps(queue), json.dumps(errors), job_id))
        self.db.commit()

    # ---- content ----
    def upsert_page(self, page: dict, chunks: list, vecs: np.ndarray | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?,?,0)",
                (page["page_id"], page.get("parent_id"), page["title"],
                 page["url"], page.get("level", 0), page.get("version"),
                 time.time(), page.get("content_hash", ""),
                 json.dumps(page.get("warnings", []))))
            old = [r[0] for r in self.db.execute(
                "SELECT chunk_id FROM chunks WHERE page_id=?", (page["page_id"],))]
            for cid in old:
                self.db.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (cid,))
                self.db.execute("DELETE FROM vectors WHERE chunk_id=?", (cid,))
            self.db.execute("DELETE FROM chunks WHERE page_id=?", (page["page_id"],))
            for i, c in enumerate(chunks):
                self.db.execute(
                    "INSERT INTO chunks VALUES (?,?,?,?,?,?,0)",
                    (c.chunk_id, page["page_id"], c.heading_path, c.kind,
                     c.ordinal, c.text))
                self.db.execute("INSERT INTO chunks_fts VALUES (?,?)", (c.chunk_id, c.text))
                if vecs is not None and i < len(vecs):
                    self.db.execute(
                        "INSERT INTO vectors VALUES (?,?)",
                        (c.chunk_id, vecs[i].astype(np.float32).tobytes()))

    def publish(self, seen_page_ids: set[str]) -> None:
        """Atomically make exactly the crawled page set the active snapshot."""
        with self.db:
            self.db.execute("UPDATE pages SET active=0")
            self.db.execute("UPDATE chunks SET active=0")
            if seen_page_ids:
                q = ",".join("?" * len(seen_page_ids))
                ids = list(seen_page_ids)
                self.db.execute(f"UPDATE pages SET active=1 WHERE page_id IN ({q})", ids)
                self.db.execute(f"UPDATE chunks SET active=1 WHERE page_id IN ({q})", ids)

    # ---- query ----
    def active_pages(self) -> dict[str, dict]:
        return {r[0]: {"title": r[1], "url": r[2], "level": r[3]}
                for r in self.db.execute(
                    "SELECT page_id,title,url,level FROM pages WHERE active=1")}

    def active_chunks(self) -> list[dict]:
        return [{"chunk_id": r[0], "page_id": r[1], "heading": r[2],
                 "kind": r[3], "text": r[4]}
                for r in self.db.execute(
                    "SELECT chunk_id,page_id,heading,kind,text FROM chunks WHERE active=1")]

    def fts_search(self, query: str, limit: int = 30) -> list[tuple[str, float]]:
        try:
            rows = self.db.execute(
                "SELECT chunk_id, rank FROM chunks_fts WHERE chunks_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (" OR ".join(f'"{w}"' for w in query.split()[:16] if w.strip()) or '""',
                 limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        active = {c["chunk_id"] for c in self.active_chunks()}
        return [(r[0], -r[1]) for r in rows if r[0] in active]

    def vectors_for(self, chunk_ids: list[str]) -> dict[str, np.ndarray]:
        if not chunk_ids:
            return {}
        q = ",".join("?" * len(chunk_ids))
        return {cid: np.frombuffer(b, dtype=np.float32)
                for cid, b in self.db.execute(
                    f"SELECT chunk_id,vec FROM vectors WHERE chunk_id IN ({q})", chunk_ids)}

    def stats(self) -> dict:
        p = self.db.execute("SELECT COUNT(*) FROM pages WHERE active=1").fetchone()[0]
        c = self.db.execute("SELECT COUNT(*) FROM chunks WHERE active=1").fetchone()[0]
        snap = self.db.execute("SELECT MAX(fetched_at) FROM pages WHERE active=1").fetchone()[0]
        return {"pages": p, "chunks": c, "as_of": snap}
