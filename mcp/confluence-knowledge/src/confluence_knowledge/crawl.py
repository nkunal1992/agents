"""Durable, resumable breadth-first crawl of one page subtree.

Queue and progress live in the index DB — a crawl can be interrupted at any
point and resumed by job ID. Bodies are fetched only for pages admitted to the
root's descendant graph.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .client import AccessError, ConfluenceClient

_PERMANENT_MARKERS = ("401", "403", "404", "permission", "not found")
# Bound a single flush's pending pop — pages are popped until ~128 chunks so
# a dense page can't put a multi-hundred-chunk buffer behind one embed call
FLUSH_MAX_CHUNKS = 128
# Battery duty cycle — pause this fraction of each micro-batch's elapsed time
# (capped 1s): scales with actual model cost, so a light model throttles
# gently while a heavy one still backs off properly
BATTERY_DUTY = 0.5


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
                 control=None, client_factory=None):
        self.client = client
        self.index = index
        self.env = env
        self.embedder = embedder  # retrieval.LocalEmbedder or None
        self.sparse = sparse      # retrieval.LocalSparseEmbedder or None
        # cancellation/heartbeat/phase/rate hook — None for tests/CLI
        self.control = control
        # token-rotation path (#177): builds a fresh client when the
        # stored credential changed — None in tests/CLI
        self._client_factory = client_factory
        self._auth_fails = 0
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

    def _auth_probe(self) -> bool:
        """Has the credential rotated? A fresh client re-reads the token
        store; success swaps it in so the crawl resumes in place."""
        if self._client_factory is None:
            return False
        try:
            client = self._client_factory()
            ep = ("wiki/rest/api/user/current" if self.env == "cloud"
                  else "rest/api/user/current")
            client.get(ep)
            self.client = client
            return True
        except Exception:  # noqa: BLE001 — still dead, keep waiting
            return False

    def _maybe_auth_pause(self, errors: list, entry: str, queue,
                          visited_submit: set) -> bool:
        """Consecutive 401s mean the token died mid-crawl — pause for a
        rotation rather than burn per-page errors (#177). On rotation the
        victim item re-queues and the doomed error lines are dropped."""
        if self._auth_fails < 1 or self.control is None:
            return False
        self.control.pause_until_auth(self._auth_probe)
        self._auth_fails = 0
        pid = entry.split(":", 1)[1] if ":" in entry else entry
        visited_submit.discard(pid)
        queue.appendleft(entry)
        errors[:] = [e for e in errors
                     if "401" not in e and "authentication failed" not in e]
        return True

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
        live = self.index.running_live_job()
        if live:
            return live                    # dedup — don't abandon in-flight
        job_id = uuid.uuid4().hex[:12]
        self.index.abandon_running_jobs()
        # a fresh job on a stale-model index clears upfront — one
        # start_crawl rebuilds; the mid-flight stamped check below stays
        # for genuinely-mid-crawl model changes (#178)
        stamped = self.index.model_id()
        if stamped is not None and stamped != _MODEL_ID:
            self.index.clear_scope()
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

        # per-phase wall-clock stats — the field's "why is the crawl slow"
        # answer. Durable under stats:{job_id} so bounded step() calls and
        # process restarts accumulate rather than reset. fetch_ms is the
        # workers' own elapsed (network), wait_ms is the main thread
        # blocked on the fetch window, embed/sparse/store/extract are the
        # CPU phases. job_progress exposes it; wait_for_crawl carries it.
        stats_raw = self.index.meta_get(f"stats:{job_id}")
        stats: dict = (json.loads(stats_raw) if stats_raw else {
            "fetch_ms": 0.0, "wait_ms": 0.0, "extract_ms": 0.0,
            "embed_ms": 0.0, "sparse_ms": 0.0, "store_ms": 0.0,
            "chunks": 0, "pages": 0})
        _stats_lock = threading.Lock()

        def _save_stats() -> None:
            stats["battery_saver"] = bool(
                self.control and self.control.battery_saver())
            stats["pages_per_s"] = round(
                stats["pages"] /
                max(stats["extract_ms"] + stats["embed_ms"]
                    + stats["store_ms"] + stats["wait_ms"], 1) * 1000, 2)
            self.index.meta_set(f"stats:{job_id}", json.dumps(stats))

        def _timed(fn, *a):
            """Fetch-worker self-timer — runs inside pool threads so the
            network cost is measured per-task, not as wall-gaps."""
            t = time.monotonic()
            try:
                return fn(*a)
            finally:
                with _stats_lock:
                    stats["fetch_ms"] += (time.monotonic() - t) * 1000

        processed = 0
        # checkpoints are throttled — the full seen/levels/queue JSON used
        # to be rewritten per page, which is O(n²) writes on a big tree.
        # A crash between checkpoints replays ≤ a few seconds of pages;
        # upsert_page is idempotent so re-staging is safe. In-flight
        # fetches are included in the checkpoint so they can't be lost.
        last_ckpt, last_ckpt_n = time.monotonic(), 0
        # #213 incremental publish: readers see a consistent growing
        # prefix instead of nothing until job end. Every KNOWLEDGE_
        # PUBLISH_EVERY pages or KNOWLEDGE_PUBLISH_EVERY_S seconds —
        # whichever first (0 pages disables). Deletions + term-stats
        # stay with the job-end publish.
        pub_every = int(os.environ.get("KNOWLEDGE_PUBLISH_EVERY", "200"))
        pub_every_s = float(
            os.environ.get("KNOWLEDGE_PUBLISH_EVERY_S", "120"))
        last_pub, last_pub_n = time.monotonic(), len(seen)
        # bounded fetch pipeline: KNOWLEDGE_FETCH_CONCURRENCY workers run
        # the network I/O (page body + child listing) while the main
        # thread does the CPU work — extract, embed (micro-batched),
        # stage. Embedding accumulates an EMBED_FLUSH-chunk buffer so a
        # burst of arrivals merges into few model calls.
        conc = limits().fetch_concurrency
        # the pool is sized at the adaptive ceiling once; the submission
        # gate below re-reads fetch_concurrency() each pass so the
        # in-flight window breathes mid-step under pressure (#179)
        pool_n = (self.control.fetch_ceiling(conc)
                  if self.control is not None else conc)
        inflight: dict = {}             # future -> entry string
        pending: list = []              # (pid, page, chunks, warns, hash)
        visited_submit: set[str] = set()  # dedupe at submit time
        # single embed/stage worker: dense embedding is the dominant serial
        # phase (~half the crawl wall) — running it beside the fetch/extract
        # loop overlaps model time with network time. One worker means at
        # most one flush in flight, so text+vector buffers stay bounded to
        # ~2×FLUSH_MAX_CHUNKS (#255)
        flush_fut = None
        flush_pids: set[str] = set()    # pages inside the in-flight flush —
                                        # checkpoints re-queue them so a
                                        # crash mid-flush re-crawls them

        def _do_flush(buf: list) -> None:
            """Embed + stage a page buffer on the embed worker; marks `seen`
            only after the rows land. Raises where the inline version
            returned False — `_join_flush` restores the bool contract."""
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
                    tick_t0 = time.monotonic()

                    def _embed_tick(done: int, total: int,
                                    _c=self.control) -> None:
                        nonlocal tick_t0
                        """Heartbeats + progress inside a dense-page embed —
                        a big flush used to freeze the visible job for
                        minutes, indistinguishable from a wedged crawl."""
                        if _c is None:
                            return
                        _c.check()           # cancel + lease heartbeat —
                                            # CrawlCancelled MUST propagate
                        _c.progress.note = \
                            f"embedding {done}/{total} chunks"
                        # battery saver, proportional to real batch cost —
                        # ~33% duty in ≤1s steps keeps the heartbeat tight
                        # and shrinks naturally on the light model; the old
                        # post-flush sleep compounded on dense pages
                        if _c.battery_saver():
                            el = time.monotonic() - tick_t0
                            time.sleep(min(el * BATTERY_DUTY, 1.0))
                            tick_t0 = time.monotonic()
                    t0 = time.monotonic()
                    import inspect as _ins
                    if "on_batch" in _ins.signature(
                            self.embedder.embed).parameters:
                        vecs = self.embedder.embed(
                            texts, on_batch=_embed_tick)
                    else:
                        vecs = self.embedder.embed(texts)
                        if self.control is not None and \
                                self.control.battery_saver():
                            # no per-batch hook on this embedder — same
                            # proportional duty pause, once per call
                            time.sleep(min(
                                (time.monotonic() - t0) * BATTERY_DUTY, 1.0))
                    stats["embed_ms"] += \
                        (time.monotonic() - t0) * 1000
                    if self.sparse is not None:
                        t0 = time.monotonic()
                        sp = self.sparse.embed(texts)
                        stats["sparse_ms"] += \
                            (time.monotonic() - t0) * 1000
            off = 0
            for pid, pg, chunks, warns, h in buf:
                n = len(chunks)
                t0 = time.monotonic()
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
                finally:
                    stats["store_ms"] += \
                        (time.monotonic() - t0) * 1000
                    stats["pages"] += 1
                off += n
            seen.update(pid for pid, *_ in buf)
            if self.control is not None:
                self.control.page_done(len(buf))
                self.control.phase("fetching")
            _save_stats()

        def _join_flush() -> bool:
            """False when the in-flight flush hit StaleJobError; other
            failures (cancel, embed) propagate like the inline version."""
            nonlocal flush_fut, flush_pids
            if flush_fut is None:
                return True
            try:
                flush_fut.result()
            except StaleJobError:
                return False
            finally:
                flush_fut = None
                flush_pids = set()
            return True

        def _flush_pending() -> bool:
            """Join the previous flush (back-pressure: ≤1 in flight), then
            hand the next ≤FLUSH_MAX_CHUNKS buffer to the embed worker."""
            nonlocal pending, flush_fut, flush_pids
            if not _join_flush():
                return False
            if not pending:
                return True
            # cap each flush at ~FLUSH_MAX_CHUNKS: pages are popped from the
            # head until the cap is hit, the rest stay pending — bounds a
            # single flush's embed time and the fetch buffer's growth even
            # when a page itself carries 200+ chunks (gap audit #2)
            buf, n_chunks = [], 0
            while pending and (n_chunks < FLUSH_MAX_CHUNKS or not buf):
                pid, pg, chunks, warns, h = pending.pop(0)
                buf.append((pid, pg, chunks, warns, h))
                n_chunks += len(chunks)
            flush_pids = {pid for pid, *_ in buf}
            flush_fut = epool.submit(_do_flush, buf)
            return True

        def _pending_chunks() -> int:
            return sum(len(chunks) for _, _, chunks, _, _ in pending)

        with ThreadPoolExecutor(max_workers=pool_n,
                                thread_name_prefix="crawl-fetch") as pool, \
                ThreadPoolExecutor(max_workers=1,
                                   thread_name_prefix="crawl-embed") as epool:
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
                        job_id, list(queue) + sorted(inflight.values())
                        + [f"page:{p}" for p in flush_pids],
                        errors, seen, levels, keep_failed)
                    last_ckpt, last_ckpt_n = time.monotonic(), processed
                if pub_every and (len(seen) - last_pub_n >= pub_every
                                  or time.monotonic() - last_pub
                                  >= pub_every_s):
                    t_pub = time.monotonic()
                    stats["inc_publish"] = stats.get("inc_publish", 0) \
                        + self.index.publish_incremental(job_id)
                    stats["publish_ms"] = stats.get("publish_ms", 0.0) \
                        + (time.monotonic() - t_pub) * 1000
                    last_pub, last_pub_n = time.monotonic(), len(seen)
                # keep the window full — never submit beyond the batch
                # bound or the adaptive cap (#179)
                cur = (self.control.fetch_concurrency(conc)
                       if self.control is not None else conc)
                while (queue and len(inflight) < cur and
                        (batch is None
                         or processed + len(inflight) < batch)):
                    entry = queue.popleft()   # "page:123" or "folder:456"
                    pid = entry.split(":", 1)[1] if ":" in entry else entry
                    if pid in visited_submit or pid in seen:
                        continue
                    visited_submit.add(pid)
                    if entry.startswith("folder:"):
                        inflight[pool.submit(
                            _timed, self._children, pid, True)] = entry
                    else:
                        inflight[pool.submit(
                            _timed, self._fetch_page, pid)] = entry
                if not inflight:
                    break
                t_wait = time.monotonic()
                done, _ = wait(tuple(inflight),
                               return_when=FIRST_COMPLETED)
                stats["wait_ms"] += (time.monotonic() - t_wait) * 1000
                for fut in done:
                    entry = inflight.pop(fut)
                    pid = entry.split(":", 1)[1] if ":" in entry else entry
                    processed += 1
                    try:
                        result = fut.result()
                    except AccessError as e:
                        if ("401" in str(e)
                                or "authentication failed" in str(e)):
                            # token died mid-crawl — pause for rotation;
                            # on rotate the item re-queues (#177)
                            self._auth_fails += 1
                            if self._maybe_auth_pause(
                                    errors, entry, queue, visited_submit):
                                continue
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
                    if not isinstance(page, AccessError):
                        self._auth_fails = 0
                        # a fetched body proves the token is alive; a dual
                        # AccessError raise (token-dead signature) lands
                        # in the except branch and counts instead
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
                        t_ex = time.monotonic()
                        chunks, warns = extract_chunks(
                            page["storage"], page_title=page.get("title", ""),
                            **self._chunk_kwargs())
                        stats["extract_ms"] += \
                            (time.monotonic() - t_ex) * 1000
                        stats["chunks"] += len(chunks)
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
            # drain the buffer the fetch loop left behind, then join the
            # last in-flight flush — publish must not run over half-staged
            # pages (inside the `with`: the embed pool is still open)
            while pending:
                if not _flush_pending():
                    _save_stats()
                    return {"job_id": job_id, "state": "abandoned",
                            "remaining": len(queue), "pages_seen": len(seen),
                            "errors": errors}
            if not _join_flush():
                _save_stats()
                return {"job_id": job_id, "state": "abandoned",
                        "remaining": len(queue), "pages_seen": len(seen),
                        "errors": errors}

        _save_stats()
        self._checkpoint(job_id, list(queue) + sorted(inflight.values())
                         + [f"page:{p}" for p in flush_pids],
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
            if pub_every:
                # step boundary with work remaining — flush staged pages
                # to readers before handing back (#213)
                t_pub = time.monotonic()
                stats["inc_publish"] = stats.get("inc_publish", 0) \
                    + self.index.publish_incremental(job_id)
                stats["publish_ms"] = stats.get("publish_ms", 0.0) \
                    + (time.monotonic() - t_pub) * 1000
            self.index.save_job(job_id, "running",
                                list(queue)
                                + [f"page:{p}" for p in flush_pids],
                                errors)
            state = "running"
        else:
            if self.control is not None:
                self.control.phase("publishing")
                self.control.progress.remaining = 0
                self.control.progress.pages_seen = len(seen)
                self.control.progress.errors_n = len(errors)
            t_pub = time.monotonic()
            pub = self.index.publish(seen | keep_failed, job_id=job_id)
            stats["publish_ms"] = stats.get("publish_ms", 0.0) + \
                (time.monotonic() - t_pub) * 1000
            _save_stats()
            # auditable outcome: what the refresh added vs tombstoned vs
            # carried forward — stored so status/last_crawl can report it
            self.index.meta_set(f"publish:{job_id}", json.dumps(pub))
            self.index.set_model_id(_MODEL_ID)
            self.index.save_job(job_id, "done", [], errors)
            state = "done"
        return {"job_id": job_id, "state": state, "remaining": len(queue),
                "pages_seen": len(seen), "errors": errors}
