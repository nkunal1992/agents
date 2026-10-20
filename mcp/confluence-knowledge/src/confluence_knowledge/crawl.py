"""Durable, resumable breadth-first crawl of one page subtree.

Queue and progress live in the index DB — a crawl can be interrupted at any
point and resumed by job ID. Bodies are fetched only for pages admitted to the
root's descendant graph.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .client import AccessError, ConfluenceClient

_PERMANENT_MARKERS = ("401", "403", "404", "permission", "not found")


def _permanent(err: Exception) -> bool:
    """4xx/permission errors are definitive — don't keep stale index rows
    for content we were authoritatively denied. 5xx/timeouts are transient:
    keep the last-known-good content."""
    return any(m in str(err) for m in _PERMANENT_MARKERS)
from .extract import extract_chunks
from .index import Index, StaleJobError
from .resources import limits


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


def _abs_url(webui: str, base: str | None) -> str:
    """Join Confluence's relative `_links.webui` onto a base so citations are
    clickable; absolute links pass through unchanged."""
    if not webui or urlparse(webui).scheme or not base:
        return webui
    return base.rstrip("/") + "/" + webui.lstrip("/")


def _fetch_page_v2(client: ConfluenceClient, page_id: str) -> dict:
    d = client.get(f"wiki/api/v2/pages/{page_id}",
                   params={"body-format": "storage"}).json()
    links = d.get("_links") or {}
    return {
        "page_id": str(d["id"]), "title": d.get("title", ""),
        "storage": (d.get("body") or {}).get("storage", {}).get("value", ""),
        "version": str((d.get("version") or {}).get("number", "")),
        "url": _abs_url(links.get("webui", ""), links.get("base")),
    }


def _fetch_page_dc(client: ConfluenceClient, page_id: str) -> dict:
    d = client.get(f"rest/api/content/{page_id}",
                   params={"expand": "body.storage,version,ancestors"}).json()
    links = d.get("_links") or {}
    return {
        "page_id": str(d["id"]), "title": d.get("title", ""),
        "storage": (d.get("body") or {}).get("storage", {}).get("value", ""),
        "version": str((d.get("version") or {}).get("number", "")),
        "url": _abs_url(links.get("webui", ""), links.get("base")),
        "ancestors": [str(a["id"]) for a in d.get("ancestors", [])],
    }


from .retrieval import CHUNK_TOKENS
from .retrieval import MODEL_MARKER as _MODEL_ID


class Crawler:
    def __init__(self, client: ConfluenceClient, index: Index, env: str,
                 embedder=None, sparse=None, site_url: str | None = None,
                 control=None):
        self.client = client
        self.index = index
        self.env = env
        self.embedder = embedder  # retrieval.LocalEmbedder or None
        self.sparse = sparse      # retrieval.LocalSparseEmbedder or None
        # cancellation/heartbeat/phase/rate hook — None for tests/CLI
        self.control = control
        # fallback base for page links when a response carries no _links.base
        if site_url and env == "cloud" and not site_url.rstrip("/").endswith("/wiki"):
            site_url = site_url.rstrip("/") + "/wiki"
        self.site_url = site_url

    def _chunk_kwargs(self) -> dict:
        """Token-aware split params when the embedder can measure tokens;
        word-based fallback keeps tests/offline extraction working."""
        measure = getattr(self.embedder, "count_tokens", None)
        return {"measure": measure, "budget": CHUNK_TOKENS} if measure else {}

    def _children(self, pid: str, is_folder: bool = False):
        if self.env == "cloud":
            return (_fetch_folder_children_v2(self.client, pid) if is_folder
                    else _fetch_children_v2(self.client, pid))
        return _fetch_children_dc(self.client, pid)

    def _page(self, pid: str) -> dict:
        page = (_fetch_page_v2(self.client, pid) if self.env == "cloud"
                else _fetch_page_dc(self.client, pid))
        page["url"] = _abs_url(page["url"], self.site_url)
        return page

    def _fetch_page(self, pid: str):
        """Worker task: page body and child listing fetched independently —
        a denied/failed body must not preclude listing children (#170: DC
        permissions are per-page; the listing endpoint decides itself).
        Raises only when both calls fail."""
        try:
            page = self._page(pid)
        except AccessError as e:
            page = e
        try:
            kids = self._children(pid, is_folder=False)
        except AccessError as e:
            kids = e
        if isinstance(page, AccessError) and isinstance(kids, AccessError):
            raise AccessError(f"{page}; children: {kids}")
        return page, kids

    def start(self, root_page: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        self.index.abandon_running_jobs()
        self.index.create_job(job_id, root_page)
        self.index.save_job(job_id, "running", [f"page:{root_page}"], [])
        return job_id

    def _checkpoint(self, job_id, queue, errors, seen, levels,
                    keep=()) -> None:
        """Persist progress so an interrupted step loses no completed page —
        including the durable keep-set of transient-failed pages."""
        with self.index.lock, self.index.db:
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"seen:{job_id}", json.dumps(sorted(seen))))
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"levels:{job_id}", json.dumps(levels)))
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"keep:{job_id}", json.dumps(sorted(keep))))
            self.index.db.execute(
                "UPDATE jobs SET queue=?, errors=? WHERE job_id=?",
                (json.dumps(queue), json.dumps(errors), job_id))
            if self.control is not None:
                # persisted so OTHER server processes (second IDE window)
                # can read progress without the in-memory registry
                self.index.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES (?,?)",
                    (f"progress:{job_id}",
                     json.dumps(self.control.progress.snapshot())))

    def step(self, job_id: str, batch: int | None = 10,
             budget_s: float | None = None) -> dict:
        """Process up to `batch` pages (None = whole queue), stopping early
        once `budget_s` seconds have passed. The background worker calls this
        unbounded; a bounded call stays under the IDE's request timeout."""
        deadline = None if budget_s is None else time.monotonic() + budget_s
        job = self.index.job(job_id)
        if job is None:
            raise AccessError(f"unknown job {job_id}")
        if job["state"] != "running":
            return {"job_id": job_id, "state": job["state"],
                    "remaining": len(job["queue"]), "errors": job["errors"]}

        queue: deque[str] = deque(job["queue"])
        errors: list[str] = job["errors"]
        # pages that fail with deny/gone errors are meant to drop out of the
        # snapshot; transient failures keep their previously-indexed content.
        # The set is durable in the job checkpoint so it survives bounded
        # steps and process restarts.
        keep_raw = self.index.meta_get(f"keep:{job_id}")
        keep_failed: set[str] = set(json.loads(keep_raw)) if keep_raw else set()

        # Mid-crawl model change: already-embedded pages live in the OLD
        # vector space — finishing this job would publish a mixed-dimension
        # index. Wipe the scope and fail the job; start_crawl rebuilds from
        # roots under the new model.
        stamped = self.index.model_id()
        if stamped is not None and stamped != _MODEL_ID and queue:
            self.index.clear_scope()
            errors = errors + [
                (f"model changed mid-crawl ({stamped} → {_MODEL_ID}) — "
                 "index cleared; run start_crawl")]
            self.index.save_job(job_id, "error", [], errors)
            return {"job_id": job_id, "state": "error",
                    "remaining": 0, "errors": errors}

        seen_raw = self.index.meta_get(f"seen:{job_id}")
        seen: set[str] = set(json.loads(seen_raw)) if seen_raw else set()
        levels_raw = self.index.meta_get(f"levels:{job_id}")
        levels: dict[str, int] = (json.loads(levels_raw) if levels_raw
                                  else {job["root_page"]: 0})

        processed = 0
        # checkpoints are throttled — the full seen/levels/queue JSON used
        # to be rewritten per page, which is O(n²) writes on a big tree.
        # A crash between checkpoints replays ≤ a few seconds of pages;
        # upsert_page is idempotent so re-staging is safe. In-flight
        # fetches are included in the checkpoint so they can't be lost.
        last_ckpt, last_ckpt_n = time.monotonic(), 0
        # bounded fetch pipeline: KNOWLEDGE_FETCH_CONCURRENCY workers run
        # the network I/O (page body + child listing) while the main
        # thread does the CPU work — extract, embed (micro-batched),
        # stage. Embedding accumulates an EMBED_FLUSH-chunk buffer so a
        # burst of arrivals merges into few model calls.
        conc = limits().fetch_concurrency
        if self.control is not None:
            conc = self.control.fetch_concurrency(conc)
        inflight: dict = {}             # future -> entry string
        pending: list = []              # (pid, page, chunks, warns, hash)
        visited_submit: set[str] = set()  # dedupe at submit time

        def _flush_pending() -> bool:
            """Stage all buffered pages; False on StaleJobError. `seen` is
            updated only after the rows land — a checkpoint taken while
            pages sit in the buffer must re-crawl them on resume."""
            nonlocal pending
            if not pending:
                return True
            buf, pending = pending, []
            vecs = sp = None
            if self.embedder is not None:
                if self.control is not None:
                    self.control.check()
                    self.control.phase("embedding"
                                       + (" (battery saver)" if
                                          self.control.battery_saver()
                                          else ""))
                texts = [f"{pg['title']}\n{c.heading_path}\n{c.text}"
                         for _, pg, chunks, _, _ in buf for c in chunks]
                if texts:
                    t0 = time.monotonic()
                    vecs = self.embedder.embed(texts)
                    if self.sparse is not None:
                        sp = self.sparse.embed(texts)
                    if self.control is not None and \
                            self.control.battery_saver():
                        # ~50% duty cycle while on battery — halves sustained
                        # CPU/heat; sessions can't retune threads post-build
                        time.sleep(max(0.0, time.monotonic() - t0))
            off = 0
            for pid, pg, chunks, warns, h in buf:
                n = len(chunks)
                try:
                    self.index.upsert_page({
                        "page_id": pid, "parent_id": None,
                        "title": pg["title"], "url": pg["url"],
                        "level": levels.get(pid, 0),
                        "version": pg["version"], "content_hash": h,
                        "warnings": warns}, chunks,
                        vecs[off:off + n] if vecs is not None else None,
                        sp[off:off + n] if sp is not None else None,
                        job_id=job_id)
                except StaleJobError:
                    return False
                off += n
            seen.update(pid for pid, *_ in buf)
            if self.control is not None:
                self.control.page_done(len(buf))
                self.control.phase("fetching")
            return True

        def _pending_chunks() -> int:
            return sum(len(chunks) for _, _, chunks, _, _ in pending)

        with ThreadPoolExecutor(max_workers=conc,
                                thread_name_prefix="crawl-fetch") as pool:
            while (queue or inflight) and (
                    batch is None or processed < batch) and (
                    deadline is None or processed == 0
                    or time.monotonic() < deadline):
                if self.control is not None:
                    self.control.check()     # cancel → heartbeat → mem guard
                    self.control.progress.remaining = \
                        len(queue) + len(inflight)
                    self.control.progress.pages_seen = len(seen)
                    self.control.progress.errors_n = len(errors)
                if processed and (time.monotonic() - last_ckpt >= 5.0
                                  or processed - last_ckpt_n >= 50):
                    self._checkpoint(
                        job_id, list(queue) + sorted(inflight.values()),
                        errors, seen, levels, keep_failed)
                    last_ckpt, last_ckpt_n = time.monotonic(), processed
                # keep the window full — never submit beyond the batch bound
                while (queue and len(inflight) < conc and
                        (batch is None
                         or processed + len(inflight) < batch)):
                    entry = queue.popleft()   # "page:123" or "folder:456"
                    pid = entry.split(":", 1)[1] if ":" in entry else entry
                    if pid in visited_submit or pid in seen:
                        continue
                    visited_submit.add(pid)
                    if entry.startswith("folder:"):
                        inflight[pool.submit(
                            self._children, pid, True)] = entry
                    else:
                        inflight[pool.submit(
                            self._fetch_page, pid)] = entry
                if not inflight:
                    break
                done, _ = wait(tuple(inflight),
                               return_when=FIRST_COMPLETED)
                for fut in done:
                    entry = inflight.pop(fut)
                    pid = entry.split(":", 1)[1] if ":" in entry else entry
                    processed += 1
                    try:
                        result = fut.result()
                    except AccessError as e:
                        errors.append(f"page {pid}: {e}")
                        if entry.startswith("folder:"):
                            # the listing died — its whole subtree is now
                            # unreachable; keep all live rows rather than
                            # let publish delete pages we never saw
                            if not _permanent(e):
                                keep_failed.update(
                                    self.index.active_pages())
                        elif not _permanent(e):
                            keep_failed.add(pid)
                        continue
                    except Exception as e:  # noqa: BLE001
                        # transport-level failure (timeout, conn reset) —
                        # transient: keep prior content, don't kill a 2h
                        # crawl over one socket hiccup
                        errors.append(
                            f"page {pid}: {type(e).__name__}: {e}")
                        if entry.startswith("folder:"):
                            keep_failed.update(self.index.active_pages())
                        else:
                            keep_failed.add(pid)
                        continue
                    if entry.startswith("folder:"):
                        # traverse folders to reach descendant pages —
                        # folders themselves are never indexed
                        for cid, ctype in result:
                            if cid not in seen:
                                queue.append(f"{ctype}:{cid}")
                        continue
                    page, kids = result
                    if isinstance(kids, AccessError):
                        # page body fetched but its child listing failed —
                        # descendants are now undiscovered; keep all live
                        # rows (no subtree mapping exists to be narrower)
                        errors.append(f"children of {pid}: {kids}")
                        keep_failed.update(self.index.active_pages())
                        kids = []
                    if isinstance(page, AccessError):
                        # body denied/failed — but a discovered child
                        # listing still flows through; each child verifies
                        # its own root ancestry when fetched
                        errors.append(f"page {pid}: {page}")
                        if not _permanent(page):
                            keep_failed.add(pid)
                        for cid, ctype in kids:
                            if cid not in seen:
                                levels.setdefault(cid,
                                                  levels.get(pid, 0) + 1)
                                queue.append(f"{ctype}:{cid}")
                        continue
                    if pid != job["root_page"]:
                        anc = page.get("ancestors") or []
                        if anc and job["root_page"] not in anc:
                            errors.append(
                                f"page {pid}: outside root subtree — "
                                "skipped")
                            continue
                    content_hash = hashlib.sha256(
                        page["storage"].encode()).hexdigest()
                    # unchanged page: reuse its live rows instead of
                    # re-extracting + re-embedding identical content
                    if (self.index.live_page_hash(pid) == content_hash
                            and self.index.copy_live_to_stage(pid,
                                                              job_id)):
                        seen.add(pid)
                        if self.control is not None:
                            # unchanged pages are "done" too — a refresh
                            # must show progress, not sit at 0 (#171)
                            self.control.page_done(1)
                    else:
                        chunks, warns = extract_chunks(
                            page["storage"], **self._chunk_kwargs())
                        for c in chunks:
                            c.chunk_id = \
                                f"{pid}:{content_hash[:8]}:{c.ordinal}"
                        pending.append(
                            (pid, page, chunks, warns, content_hash))
                        if _pending_chunks() >= 64 and not _flush_pending():
                            return {"job_id": job_id,
                                    "state": "abandoned",
                                    "remaining": len(queue),
                                    "pages_seen": len(seen),
                                    "errors": errors}
                    for cid, ctype in kids:
                        if cid not in seen:
                            levels.setdefault(cid,
                                              levels.get(pid, 0) + 1)
                            queue.append(f"{ctype}:{cid}")
            # drain any stragglers still in flight when the batch bound hit
            for fut, entry in inflight.items():
                try:
                    fut.result()          # already done or completes soon
                except Exception as e:  # noqa: BLE001 — unprocessed anyway
                    errors.append(
                        f"prefetch {entry}: "
                        f"{type(e).__name__} (refetches on resume)")
        if not _flush_pending():
            return {"job_id": job_id, "state": "abandoned",
                    "remaining": len(queue), "pages_seen": len(seen),
                    "errors": errors}

        self._checkpoint(job_id, list(queue) + sorted(inflight.values()),
                         errors, seen, levels, keep_failed)

        # fence: a newer crawl may have superseded this job while a fetch was
        # in flight — never let an abandoned job publish or resurrect itself
        current = self.index.job(job_id)
        if current is None or current["state"] != "running":
            return {"job_id": job_id,
                    "state": current["state"] if current else "abandoned",
                    "remaining": len(queue), "pages_seen": len(seen),
                    "errors": errors}
        if queue:
            self.index.save_job(job_id, "running", list(queue), errors)
            state = "running"
        else:
            if self.control is not None:
                self.control.phase("publishing")
                self.control.progress.remaining = 0
                self.control.progress.pages_seen = len(seen)
                self.control.progress.errors_n = len(errors)
            self.index.publish(seen | keep_failed, job_id=job_id)
            self.index.set_model_id(_MODEL_ID)
            self.index.save_job(job_id, "done", [], errors)
            state = "done"
        return {"job_id": job_id, "state": state, "remaining": len(queue),
                "pages_seen": len(seen), "errors": errors}
