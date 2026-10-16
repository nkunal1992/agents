"""Durable, resumable breadth-first crawl of one page subtree.

Queue and progress live in the index DB — a crawl can be interrupted at any
point and resumed by job ID. Bodies are fetched only for pages admitted to the
root's descendant graph.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field

from .client import AccessError, ConfluenceClient
from .extract import extract_chunks
from .index import Index


@dataclass
class CrawlJob:
    job_id: str
    root_page: str
    queue: list[str] = field(default_factory=list)
    visited: set[str] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)


def _fetch_children_v2(client: ConfluenceClient, page_id: str) -> list[tuple[str, str]]:
    """(child_id, child_type) — pages and folders both; folders are traversed
    but never indexed as content."""
    out, cursor = [], None
    while True:
        params = {"limit": 250}
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"wiki/api/v2/pages/{page_id}/direct-children", params)
        d = r.json()
        for ch in d.get("results", []):
            out.append((str(ch["id"]), ch.get("type", "page")))
        nxt = (d.get("_links") or {}).get("next")
        if not nxt:
            break
        cursor = nxt.split("cursor=")[-1].split("&")[0]
    return out


def _fetch_folder_children_v2(client: ConfluenceClient, folder_id: str) -> list[tuple[str, str]]:
    out, cursor = [], None
    while True:
        params = {"limit": 250}
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"wiki/api/v2/folders/{folder_id}/direct-children", params)
        d = r.json()
        for ch in d.get("results", []):
            out.append((str(ch["id"]), ch.get("type", "page")))
        nxt = (d.get("_links") or {}).get("next")
        if not nxt:
            break
        cursor = nxt.split("cursor=")[-1].split("&")[0]
    return out


def _fetch_children_dc(client: ConfluenceClient, page_id: str) -> list[tuple[str, str]]:
    out, start = [], 0
    while True:
        r = client.get(f"rest/api/content/{page_id}/child/page",
                       params={"limit": 200, "start": start})
        d = r.json()
        for ch in d.get("results", []):
            out.append((str(ch["id"]), "page"))
        if d.get("size", 0) < d.get("limit", 200):
            break
        start += d["size"]
    return out


def _fetch_page_v2(client: ConfluenceClient, page_id: str) -> dict:
    d = client.get(f"wiki/api/v2/pages/{page_id}",
                   params={"body-format": "storage"}).json()
    return {
        "page_id": str(d["id"]), "title": d.get("title", ""),
        "storage": (d.get("body") or {}).get("storage", {}).get("value", ""),
        "version": str((d.get("version") or {}).get("number", "")),
        "url": (d.get("_links") or {}).get("webui", ""),
    }


def _fetch_page_dc(client: ConfluenceClient, page_id: str) -> dict:
    d = client.get(f"rest/api/content/{page_id}",
                   params={"expand": "body.storage,version,ancestors"}).json()
    webui = (d.get("_links") or {}).get("webui", "")
    base = (d.get("_links") or {}).get("base", "")
    return {
        "page_id": str(d["id"]), "title": d.get("title", ""),
        "storage": (d.get("body") or {}).get("storage", {}).get("value", ""),
        "version": str((d.get("version") or {}).get("number", "")),
        "url": base + webui if webui.startswith("/") else webui,
        "ancestors": [str(a["id"]) for a in d.get("ancestors", [])],
    }


class Crawler:
    def __init__(self, client: ConfluenceClient, index: Index, env: str,
                 embedder=None):
        self.client = client
        self.index = index
        self.env = env
        self.embedder = embedder  # retrieval.LocalEmbedder or None

    def _children(self, pid: str, is_folder: bool = False):
        if self.env == "cloud":
            return (_fetch_folder_children_v2(self.client, pid) if is_folder
                    else _fetch_children_v2(self.client, pid))
        return _fetch_children_dc(self.client, pid)

    def _page(self, pid: str) -> dict:
        return (_fetch_page_v2(self.client, pid) if self.env == "cloud"
                else _fetch_page_dc(self.client, pid))

    def start(self, root_page: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        self.index.create_job(job_id, root_page)
        self.index.save_job(job_id, "running", [f"page:{root_page}"], [])
        return job_id

    def step(self, job_id: str, batch: int = 10) -> dict:
        job = self.index.job(job_id)
        if job is None:
            raise AccessError(f"unknown job {job_id}")
        if job["state"] != "running":
            return {"job_id": job_id, "state": job["state"],
                    "remaining": len(job["queue"]), "errors": job["errors"]}

        queue: list[str] = job["queue"]
        errors: list[str] = job["errors"]
        seen_file = self.index.db.execute(
            "SELECT value FROM meta WHERE key=?", (f"seen:{job_id}",)).fetchone()
        seen: set[str] = set(json.loads(seen_file[0])) if seen_file else set()
        levels_file = self.index.db.execute(
            "SELECT value FROM meta WHERE key=?", (f"levels:{job_id}",)).fetchone()
        levels: dict[str, int] = (json.loads(levels_file[0]) if levels_file
                                  else {job["root_page"]: 0})
        visited: set[str] = set()

        processed = 0
        while queue and processed < batch:
            entry = queue.pop(0)          # "page:123" or "folder:456"
            is_folder = entry.startswith("folder:")
            pid = entry.split(":", 1)[1] if ":" in entry else entry
            if pid in visited or pid in seen:
                continue
            visited.add(pid)
            processed += 1
            if is_folder:
                # traverse folders to reach descendant pages — never indexed
                for cid, ctype in self._children(pid, is_folder=True):
                    if cid not in seen:
                        queue.append(f"{ctype}:{cid}")
                continue
            try:
                page = self._page(pid)
            except AccessError as e:
                errors.append(f"page {pid}: {e}")
                continue
            if pid != job["root_page"]:
                anc = page.get("ancestors") or []
                if anc and job["root_page"] not in anc:
                    errors.append(f"page {pid}: outside root subtree — skipped")
                    continue
            content_hash = hashlib.sha256(
                page["storage"].encode()).hexdigest()
            chunks, warns = extract_chunks(page["storage"])
            for c in chunks:
                c.chunk_id = f"{pid}:{content_hash[:8]}:{c.ordinal}"
            vecs = None
            if self.embedder is not None and chunks:
                vecs = self.embedder.embed(
                    [f"{c.heading}\n{c.text}" for c in chunks])
            self.index.upsert_page({
                "page_id": pid, "parent_id": None, "title": page["title"],
                "url": page["url"], "level": levels.get(pid, 0),
                "version": page["version"], "content_hash": content_hash,
                "warnings": warns}, chunks, vecs)
            seen.add(pid)
            for cid, ctype in self._children(pid, is_folder=False):
                if cid not in seen:
                    levels.setdefault(cid, levels.get(pid, 0) + 1)
                    queue.append(f"{ctype}:{cid}")

        # persist progress — resumable across interruptions
        self.index.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            (f"seen:{job_id}", json.dumps(sorted(seen))))
        self.index.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            (f"levels:{job_id}", json.dumps(levels)))
        self.index.db.commit()

        if queue:
            self.index.save_job(job_id, "running", queue, errors)
            state = "running"
        else:
            self.index.publish(seen)
            self.index.save_job(job_id, "done", [], errors)
            state = "done"
        return {"job_id": job_id, "state": state, "remaining": len(queue),
                "pages_seen": len(seen), "errors": errors}
