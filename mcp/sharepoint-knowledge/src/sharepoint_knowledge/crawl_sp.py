"""Durable BFS over a SharePoint site (pages + document libraries).

Queue entries: site:<id> → list:<list_id> + drive:<drive_id>;
list:<id> → page:<item_id>…; drive:<id> → item:<drive_id>:<item_id>…
File bytes are routed through the shared extract_files (docx/pdf/pptx/xlsx/
csv/txt/md/html). Site-page canvas HTML goes through extract_chunks.
Only items reachable through the chosen root are ever fetched.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.parse import urlparse

from confluence_knowledge.client import AccessError, ConfluenceClient
from confluence_knowledge.extract import extract_chunks
from confluence_knowledge.extract_files import extract_file_chunks, is_secret_name
from confluence_knowledge.index import Index, StaleJobError
from confluence_knowledge.resources import limits
from confluence_knowledge.settings import SetupError

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 20_000
FLUSH_MAX_CHUNKS = 128   # chunks per embed flush — bounds each call
BATTERY_DUTY = 0.5       # battery saver duty ratio, ~proportional steps

from confluence_knowledge.retrieval import CHUNK_TOKENS
from confluence_knowledge.retrieval import MODEL_MARKER as _MODEL_ID


def resolve_root(client: ConfluenceClient, url: str) -> tuple[str, str]:
    """SharePoint URL → (root queue entry, site webUrl for citations).

    Accepts:
      https://host.sharepoint.com/sites/name  → whole site (pages+libraries)
      …/sites/name/<library or folder path>   → that subtree only
      https://host.sharepoint.com             → root site
    """
    u = urlparse(url.strip())
    if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1"):
        raise SetupError("SharePoint URL must be https")
    host = u.hostname
    path = u.path.rstrip("/") or "/"

    # site path = /sites/{name} or /teams/{name} (or root "/")
    site_path, item_path = "/", ""
    parts = [p for p in path.split("/") if p]
    if parts and parts[0] in ("sites", "teams"):
        site_path = "/" + "/".join(parts[:2])
        item_path = "/".join(parts[2:])
        site_path = site_path.rstrip("/")

    if site_path == "/":
        site = client.get(f"sites/{host}").json()
    else:
        site = client.get(f"sites/{host}:{site_path}").json()
    site_id = site["id"]
    site_web = site.get("webUrl", url)

    if item_path:
        item = client.get(
            f"sites/{site_id}/drive/root:/{item_path}").json()
        drive = (item.get("parentReference") or {}).get("driveId") or \
            client.get(f"sites/{site_id}/drive").json()["id"]
        return f"item:{drive}:{item['id']}", site_web
    return f"site:{site_id}", site_web


def _paged(client: ConfluenceClient, path: str, params: dict | None = None):
    """Follow @odata.nextLink — absolute links are validated inside the
    client's origin+base by _validate_path, so pagination stays in scope.
    A repeated next link is a server-side loop — stop rather than crawl
    the same page forever."""
    seen_links: set[str] = set()
    while True:
        r = client.get(path, params)
        d = r.json()
        yield from d.get("value", [])
        nxt = d.get("@odata.nextLink")
        if not nxt or nxt in seen_links:
            return
        seen_links.add(nxt)
        path, params = nxt, None


def _expand_list_entries(client: ConfluenceClient, site_id: str,
                         list_id: str) -> list[str]:
    """Fetch worker: enumerate a Site Pages list into page: queue entries."""
    return [f"page:{site_id}:{list_id}:{it['id']}"
            for it in _paged(client,
                             f"sites/{site_id}/lists/{list_id}/items")]


class SPCrawler:
    def __init__(self, client: ConfluenceClient, index: Index, embedder=None,
                 downloader=None, sparse=None, control=None):
        self.client = client
        self.index = index
        self.embedder = embedder
        self.sparse = sparse
        self.downloader = downloader  # injected for tests; real one in server
        self.control = control        # cancellation/heartbeat/phase hook

    def _chunk_kwargs(self) -> dict:
        measure = getattr(self.embedder, "count_tokens", None)
        return {"measure": measure, "budget": CHUNK_TOKENS} if measure else {}

    # ---- expansions ----

    def _expand_site(self, site_id: str) -> tuple[list[str], list[str]]:
        """site → the Site Pages list + every document library."""
        out, warns = [], []
        for lst in _paged(self.client, f"sites/{site_id}/lists"):
            name = lst.get("displayName", "")
            base = (lst.get("list") or {}).get("template", "")
            if name == "Site Pages" or base == "webPageLibrary":
                out.append(f"list:{site_id}:{lst['id']}")
        for drv in _paged(self.client, f"sites/{site_id}/drives"):
            out.append(f"drive:{drv['id']}")
        return out, warns

    def _expand_drive(self, drive_id: str) -> list[str]:
        return [f"item:{drive_id}:{it['id']}"
                for it in _paged(self.client, f"drives/{drive_id}/root/children")]

    def _expand_item(self, drive_id: str, item_id: str) -> list[str]:
        return [f"item:{drive_id}:{it['id']}"
                for it in _paged(self.client,
                                 f"drives/{drive_id}/items/{item_id}/children")]

    # ---- pages ----

    def _fetch_list_item(self, site_id: str, list_id: str,
                         item_id: str):
        """Fetch worker: page JSON only — extraction happens on the main
        thread (and not at all when the content_hash matches live)."""
        d = self.client.get(
            f"sites/{site_id}/lists/{list_id}/items/{item_id}",
            {"$expand": "fields"}).json()
        fields = d.get("fields", {})
        title = fields.get("Title") or d.get("name") or item_id
        canvas = fields.get("CanvasContent1") or fields.get("WikiField") or ""
        page = {"page_id": f"page:{site_id}:{list_id}:{item_id}",
                "title": title,
                "url": (d.get("webUrl")
                        or (fields.get("_dlc_DocIdUrl") or {}).get("Url", "")),
                "version": str(d.get("lastModifiedDateTime") or ""),
                "content_hash": hashlib.sha256(canvas.encode()).hexdigest()}
        return page, canvas

    def _page_from_list_item(self, site_id: str, list_id: str,
                             item_id: str) -> tuple[dict, list, list]:
        """Synchronous fetch+extract — kept for direct/test callers."""
        page, canvas = self._fetch_list_item(site_id, list_id, item_id)
        return self._finish_list_item(page, canvas)

    def _finish_list_item(self, page: dict, canvas: str):
        """Main-thread extraction for a changed list page."""
        warns: list[str] = []
        chunks = []
        if canvas:
            html = canvas
            try:
                parts = json.loads(canvas)
                if isinstance(parts, list):
                    html = "\n".join(p.get("innerHtml", "")
                                     for p in parts if isinstance(p, dict))
            except (json.JSONDecodeError, TypeError):
                pass  # raw HTML already
            chunks, warns = extract_chunks(
                html, page_title=page.get("title", ""), **self._chunk_kwargs())
        else:
            warns.append("no canvas content — page may be empty or classic")
        page["warnings"] = warns
        return page, chunks, warns

    def _fetch_item(self, drive_id: str, item_id: str):
        """Fetch worker for a drive item — returns one of:
        ("kids", entries)             folder → children queue entries
        ("unchanged", page_id)        same lastModified → reuse live rows
        ("skip", warn)                secret/no-URL/oversized disclosure
        ("file", meta, data, hash)    downloaded bytes for the main thread
        """
        d = self.client.get(
            f"drives/{drive_id}/items/{item_id}",
            {"$select": "id,name,size,webUrl,lastModifiedDateTime,"
                        "@microsoft.graph.downloadUrl"}).json()
        if d.get("folder") is not None:
            try:
                return ("kids", self._expand_item(drive_id, item_id))
            except Exception as e:  # noqa: BLE001 — child list failed
                # the folder's descendants are now undiscovered — keep the
                # drive's live rows rather than let publish delete them
                return ("folder_failed", drive_id, e)
        name = d.get("name", item_id)
        pid = f"file:{drive_id}:{item_id}"
        if is_secret_name(name):
            # disclose-and-skip — same posture as local files: the file is
            # never a page in the index, only a warning on the job
            return ("skip", f"{name}: looks like a secret — skipped")
        # unchanged version → skip the download entirely (Graph bumps
        # lastModifiedDateTime on any content change)
        ver = str(d.get("lastModifiedDateTime", ""))
        if ver and self.index.live_page_version(pid) == ver:
            return ("unchanged", pid)
        dl_url = d.get("@microsoft.graph.downloadUrl")
        if not dl_url or (d.get("size") or 0) > MAX_FILE_BYTES:
            why = ("no download URL" if not dl_url
                   else f"too large ({d['size']} bytes)")
            return ("skip", f"{name}: {why} — skipped")
        data = self.downloader(dl_url)
        meta = {"page_id": pid, "title": name, "url": d.get("webUrl", ""),
                "version": ver,
                "content_hash": hashlib.sha256(data).hexdigest()}
        return ("file", meta, data, meta["content_hash"])

    # ---- the crawl ----

    def start(self, root_entry: str) -> str:
        live = self.index.running_live_job()
        if live:
            return live                    # dedup — don't abandon in-flight
        job_id = uuid.uuid4().hex[:12]
        self.index.abandon_running_jobs()
        stamped = self.index.model_id()
        if stamped is not None and stamped != _MODEL_ID:
            self.index.clear_scope()
        self.index.create_job(job_id, root_entry)
        self.index.save_job(job_id, "running", [root_entry], [])
        return job_id

    def _checkpoint(self, job_id, queue, errors, seen, levels, warns,
                    keep=(), parents=None) -> None:
        with self.index.lock, self.index.db:
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"seen:{job_id}", json.dumps(sorted(seen))))
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"levels:{job_id}", json.dumps(levels)))
            if parents is not None:
                self.index.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES (?,?)",
                    (f"parents:{job_id}", json.dumps(parents)))
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"warns:{job_id}", json.dumps(warns)))
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"keep:{job_id}", json.dumps(sorted(keep))))
            self.index.db.execute(
                "UPDATE jobs SET queue=?, errors=? WHERE job_id=?",
                (json.dumps(queue), json.dumps(errors), job_id))
            if self.control is not None:
                self.index.db.execute(
                    "INSERT OR REPLACE INTO meta VALUES (?,?)",
                    (f"progress:{job_id}",
                     json.dumps(self.control.progress.snapshot())))

    def step(self, job_id: str) -> dict:
        job = self.index.job(job_id)
        if job is None:
            raise AccessError(f"unknown job {job_id}")
        if job["state"] != "running":
            return {"job_id": job_id, "state": job["state"],
                    "remaining": len(job["queue"]), "errors": job["errors"]}

        queue: deque[str] = deque(job["queue"])
        errors: list[str] = job["errors"]
        # deny/gone errors drop the item from the snapshot; transient
        # failures keep the previously-indexed content — durable across
        # steps and restarts via the job checkpoint
        keep_raw = self.index.meta_get(f"keep:{job_id}")
        keep_failed: set[str] = set(json.loads(keep_raw)) if keep_raw else set()
        # Mid-crawl model change: already-embedded pages live in the OLD
        # vector space — finishing would publish mixed dims. Wipe + fail;
        # start_crawl rebuilds under the new model.
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
        levels: dict[str, int] = (json.loads(levels_raw) if levels_raw else {})
        parents_raw = self.index.meta_get(f"parents:{job_id}")
        # #240: entry→parent map — the folder tree a descendent scope
        # later reads through (folders are entries, not page rows)
        parents: dict[str, str] = (json.loads(parents_raw)
                                   if parents_raw else {})
        warns_raw = self.index.meta_get(f"warns:{job_id}")
        warns: list[str] = json.loads(warns_raw) if warns_raw else []

        # per-phase wall-clock stats — parity with confluence crawl.py
        # (#244); durable under stats:{job_id}
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

        processed = 0
        # throttled checkpoints — full state used to be rewritten per item
        last_ckpt, last_ckpt_n = time.monotonic(), 0
        # bounded fetch pipeline — KNOWLEDGE_FETCH_CONCURRENCY workers run
        # Graph metadata/downloads while the main thread extracts+embeds+
        # stages; unchanged items reuse their live rows (version check
        # skips the download entirely; content_hash skips re-embedding)
        conc = limits().fetch_concurrency
        # the pool is sized at the adaptive ceiling once; the submission
        # gate re-reads fetch_concurrency() each pass so the window
        # breathes mid-step under pressure (#184, parity #179)
        pool_n = (self.control.fetch_ceiling(conc)
                  if self.control is not None else conc)
        inflight: dict = {}             # future -> entry string
        pending: list = []              # (pid, page, chunks, hash) to stage
        visited_submit: set[str] = set()

        def _flush_pending() -> bool:
            nonlocal pending
            if not pending:
                return True
            # cap each flush at ~FLUSH_MAX_CHUNKS — bounds a single
            # flush's embed time; the rest stay pending (#244, parity
            # with confluence crawl)
            buf, n_chunks = [], 0
            while pending and (n_chunks < FLUSH_MAX_CHUNKS or not buf):
                pid, pg, chunks, h = pending.pop(0)
                buf.append((pid, pg, chunks, h))
                n_chunks += len(chunks)
            vecs = sp = None
            if self.embedder is not None:
                if self.control is not None:
                    self.control.check()
                    self.control.phase("embedding"
                                       + (" (battery saver)" if
                                          self.control.battery_saver()
                                          else ""))
                texts = [f"{pg['title']}\n{c.heading_path}\n{c.text}"
                         for _, pg, chunks, _ in buf for c in chunks]
                if texts:
                    tick_t0 = time.monotonic()

                    def _embed_tick(done: int, total: int,
                                    _c=self.control) -> None:
                        nonlocal tick_t0
                        """Heartbeat + progress inside a dense flush —
                        a big item used to freeze the visible job."""
                        if _c is None:
                            return
                        _c.check()
                        _c.progress.note = \
                            f"embedding {done}/{total} chunks"
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
            t0 = time.monotonic()
            for pid, pg, chunks, h in buf:
                n = len(chunks)
                try:
                    self.index.upsert_page({
                        "page_id": pid, "parent_id": None,
                        "title": pg["title"], "url": pg["url"],
                        "level": levels.get(pid, 0),
                        "version": pg["version"], "content_hash": h,
                        "warnings": pg["warnings"]}, chunks,
                        vecs[off:off + n] if vecs is not None else None,
                        sp[off:off + n] if sp is not None else None,
                        job_id=job_id)
                except StaleJobError:
                    return False
                off += n
            stats["store_ms"] += (time.monotonic() - t0) * 1000
            stats["pages"] += len(buf)
            stats["chunks"] += n_chunks
            seen.update(pid for pid, *_ in buf)
            if self.control is not None:
                self.control.page_done(len(buf))
                self.control.phase("fetching")
            return True

        def _stage_result(pid, page, chunks) -> bool:
            """Buffer one fetched page for the next flush."""
            pending.append((pid, page, chunks,
                            page["content_hash"]))
            if sum(len(c) for _, _, c, _ in pending) >= 64:
                return _flush_pending()
            return True

        def _keep_map(entry: str) -> None:
            """Transient failure on a container/page → keep prior rows."""
            if entry.startswith("page:"):
                keep_failed.add(entry)
            elif entry.startswith("item:"):
                keep_failed.add(f"file:{entry[5:]}")
            else:
                if entry.startswith("list:"):
                    prefix = f"page:{entry[5:]}:"
                elif entry.startswith("drive:"):
                    prefix = f"file:{entry[6:]}:"
                else:
                    prefix = ""  # site: — keep everything it produced
                keep_failed.update(
                    pid for pid in self.index.active_pages()
                    if pid.startswith(prefix))

        def _timed(fn, *a):
            """Fetch-worker self-timer — measures the network cost
            per-task from inside pool threads (#244, parity crawl.py)."""
            t = time.monotonic()
            try:
                return fn(*a)
            finally:
                with _stats_lock:
                    stats["fetch_ms"] += (time.monotonic() - t) * 1000

        def _submit(pool):
            # re-read the adaptive cap each fill — the window breathes
            # mid-step under pressure (#184, parity #179)
            cur = (self.control.fetch_concurrency(conc)
                   if self.control is not None else conc)
            while queue and len(inflight) < cur:
                entry = queue.popleft()
                if entry in visited_submit:
                    continue
                visited_submit.add(entry)
                try:
                    if entry.startswith("site:"):
                        inflight[pool.submit(
                            _timed, self._expand_site, entry[5:])] = entry
                    elif entry.startswith("list:"):
                        _, site_id, list_id = entry.split(":", 2)
                        inflight[pool.submit(
                            _timed, _expand_list_entries, self.client,
                            site_id, list_id)] = entry
                    elif entry.startswith("drive:"):
                        inflight[pool.submit(
                            _timed, self._expand_drive, entry[6:])] = entry
                    elif entry.startswith("page:"):
                        _, s, l, i = entry.split(":", 3)
                        inflight[pool.submit(
                            _timed, self._fetch_list_item, s, l, i)] = entry
                    elif entry.startswith("item:"):
                        _, d, i = entry.split(":", 2)
                        inflight[pool.submit(
                            _timed, self._fetch_item, d, i)] = entry
                    else:
                        errors.append(f"unknown queue entry {entry}")
                except Exception as e:  # noqa: BLE001 — malformed entry
                    errors.append(f"{entry}: {type(e).__name__}: {e}")

        with ThreadPoolExecutor(max_workers=pool_n,
                                thread_name_prefix="crawl-fetch") as pool:
            while queue or inflight:
                if self.control is not None:
                    self.control.check()
                    self.control.progress.remaining = \
                        len(queue) + len(inflight)
                    self.control.progress.pages_seen = len(seen)
                    self.control.progress.errors_n = len(errors)
                if len(seen) >= MAX_FILES:
                    errors.append(
                        f"file cap reached ({MAX_FILES}) — remaining skipped")
                    # a cap-stopped crawl is not proof of deletion —
                    # keep every previously-indexed page we never visited
                    keep_failed.update(
                        set(self.index.active_pages()) - seen)
                    queue.clear()
                    inflight.clear()
                    break
                if processed and (time.monotonic() - last_ckpt >= 5.0
                                  or processed - last_ckpt_n >= 50):
                    self._checkpoint(
                        job_id, list(queue) + sorted(inflight.values()),
                        errors, seen, levels, warns, keep_failed,
                        parents=parents)
                    _save_stats()
                    last_ckpt, last_ckpt_n = time.monotonic(), processed
                _submit(pool)
                if not inflight:
                    break
                t_wait = time.monotonic()
                done, _ = wait(tuple(inflight),
                               return_when=FIRST_COMPLETED)
                stats["wait_ms"] += (time.monotonic() - t_wait) * 1000
                for fut in done:
                    entry = inflight.pop(fut)
                    processed += 1
                    try:
                        result = fut.result()
                    except AccessError as e:
                        errors.append(f"{entry}: {e}")
                        if not any(m in str(e) for m in
                                   ("401", "403", "404", "permission",
                                    "not found")):
                            _keep_map(entry)
                        continue
                    except Exception as e:  # noqa: BLE001 — transport hiccup
                        errors.append(f"{entry}: {type(e).__name__}: {e}")
                        _keep_map(entry)
                        continue
                    if entry.startswith(("site:", "list:", "drive:")):
                        kids, w = (result if entry.startswith("site:")
                                   else (result, []))
                        warns.extend(w)
                        for k in kids:
                            if k not in seen:
                                parents.setdefault(k, entry)
                        queue.extend(k for k in kids if k not in seen)
                        continue
                    if entry.startswith("item:"):
                        tag = result[0]
                        if tag == "kids":
                            for k in result[1]:
                                if k not in seen:
                                    parents.setdefault(k, entry)
                            queue.extend(k for k in result[1]
                                         if k not in seen)
                            continue
                        if tag == "folder_failed":
                            _, drive_id, exc = result
                            errors.append(f"{entry}: {exc}")
                            keep_failed.update(
                                pid for pid in self.index.active_pages()
                                if pid.startswith(f"file:{drive_id}:"))
                            continue
                        if tag == "skip":
                            warns.append(result[1])
                            continue
                        if tag == "unchanged":
                            if self.index.copy_live_to_stage(result[1],
                                                             job_id):
                                seen.add(result[1])
                                if self.control is not None:
                                    self.control.page_done(1)
                            continue
                        _, meta, data, h = result
                        pid = meta["page_id"]
                        if pid in seen:
                            continue
                        if (self.index.live_page_hash(pid) == h
                                and self.index.copy_live_to_stage(
                                    pid, job_id)):
                            seen.add(pid)
                            if self.control is not None:
                                self.control.page_done(1)
                            continue
                        t0 = time.monotonic()
                        chunks, w = extract_file_chunks(
                            meta["title"], data, **self._chunk_kwargs())
                        stats["extract_ms"] += \
                            (time.monotonic() - t0) * 1000
                        meta["warnings"] = w
                        warns.extend(w)
                        for c in chunks:
                            c.chunk_id = f"{pid}:{h[:8]}:{c.ordinal}"
                        if not _stage_result(pid, meta, chunks):
                            return {"job_id": job_id, "state": "abandoned",
                                    "remaining": len(queue),
                                    "pages_seen": len(seen),
                                    "errors": errors, "warnings": warns}
                        continue
                    # page: entries — (page_meta, canvas)
                    page, canvas = result
                    pid = page["page_id"]
                    if pid in seen:
                        continue
                    h = page["content_hash"]
                    if (self.index.live_page_hash(pid) == h
                            and self.index.copy_live_to_stage(pid, job_id)):
                        seen.add(pid)
                        if self.control is not None:
                            self.control.page_done(1)
                        continue
                    page, chunks, w = self._finish_list_item(page, canvas)
                    warns.extend(w)
                    for c in chunks:
                        c.chunk_id = f"{pid}:{h[:8]}:{c.ordinal}"
                    if not _stage_result(pid, page, chunks):
                        return {"job_id": job_id, "state": "abandoned",
                                "remaining": len(queue),
                                "pages_seen": len(seen),
                                "errors": errors, "warnings": warns}
            for fut, entry in inflight.items():
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001 — unprocessed anyway
                    errors.append(
                        f"prefetch {entry}: "
                        f"{type(e).__name__} (refetches on resume)")
        if not _flush_pending():
            return {"job_id": job_id, "state": "abandoned",
                    "remaining": len(queue), "pages_seen": len(seen),
                    "errors": errors, "warnings": warns}

        self._checkpoint(job_id, list(queue) + sorted(inflight.values()),
                         errors, seen, levels, warns, keep_failed,
                         parents=parents)
        # fence: an abandoned/superseded job must never publish or resurrect
        current = self.index.job(job_id)
        if current is None or current["state"] != "running":
            return {"job_id": job_id,
                    "state": current["state"] if current else "abandoned",
                    "remaining": len(queue), "pages_seen": len(seen),
                    "errors": errors, "warnings": warns}
        if not queue:
            if self.control is not None:
                self.control.phase("publishing")
                self.control.progress.remaining = 0
                self.control.progress.pages_seen = len(seen)
                self.control.progress.errors_n = len(errors)
            pub = self.index.publish(seen | keep_failed, job_id=job_id)
            # the published entry tree — a later connect() on a folder or
            # page inside this scope reads through it (#240)
            self.index.meta_set("entry_parents", json.dumps(parents))
            # auditable outcome: what the refresh added vs tombstoned vs
            # carried forward — stored so status/last_crawl can report it
            self.index.meta_set(f"publish:{job_id}", json.dumps(pub))
            self.index.set_model_id(_MODEL_ID)
            _save_stats()
        self.index.save_job(job_id, "done" if not queue else "running",
                            list(queue), errors)
        return {"job_id": job_id,
                "state": "done" if not queue else "running",
                "remaining": len(queue), "pages_seen": len(seen),
                "errors": errors, "warnings": warns}
