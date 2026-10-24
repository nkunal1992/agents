"""Durable, resumable filesystem crawl — one path (file or folder) → index.

Same durable-queue/checkpoint contract as the Confluence crawler: progress
survives interruption, the background worker runs to completion, and only a
completed crawl publishes (a partial refresh never clobbers a good snapshot).
Reads only — never writes, follows no symlinks outside the root.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from confluence_knowledge.extract_files import extract_file_chunks, is_secret_name
from confluence_knowledge.index import Index, StaleJobError
from confluence_knowledge.resources import limits
from confluence_knowledge.settings import SetupError

# dirs never worth indexing — noise or actively harmful to read
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv",
             "venv", ".idea", ".vscode", "target", "dist", "build", ".tox"}
MAX_FILE_BYTES = 25 * 1024 * 1024  # 25MB — bigger files get a warning, not a crash
FLUSH_MAX_CHUNKS = 128   # chunks per embed flush — bounds each call
BATTERY_DUTY = 0.5       # battery saver duty ratio, ~proportional steps
MAX_FILES = 20_000                  # guard against "pointed at $HOME" accidents


def resolve_root(path: str) -> Path:
    """Validate + canonicalize a user-supplied path (file or folder)."""
    p = Path(path).expanduser()
    if not p.exists():
        raise SetupError(f"path does not exist: {path}")
    return p.resolve()


from confluence_knowledge.retrieval import CHUNK_TOKENS
from confluence_knowledge.retrieval import MODEL_MARKER as _MODEL_ID


class LocalCrawler:
    def __init__(self, index: Index, root: Path, embedder=None, sparse=None,
                 control=None):
        self.index = index
        self.root = root      # realpath — everything indexed must live under it
        self.embedder = embedder
        self.sparse = sparse
        self.control = control  # cancellation/heartbeat/phase/rate hook

    def _chunk_kwargs(self) -> dict:
        measure = getattr(self.embedder, "count_tokens", None)
        return {"measure": measure, "budget": CHUNK_TOKENS} if measure else {}

    def start(self) -> str:
        live = self.index.running_live_job()
        if live:
            return live                    # dedup — don't abandon in-flight
        job_id = uuid.uuid4().hex[:12]
        self.index.abandon_running_jobs()
        stamped = self.index.model_id()
        if stamped is not None and stamped != _MODEL_ID:
            self.index.clear_scope()
        self.index.create_job(job_id, str(self.root))
        first = f"dir:{self.root}" if self.root.is_dir() else f"file:{self.root}"
        self.index.save_job(job_id, "running", [first], [])
        return job_id

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root.parent)).replace("\\", "/")
        except ValueError:
            return path.name

    def _children(self, d: Path) -> tuple[list[str], list[str]]:
        """(new queue entries, warnings) — guarded, secret-aware listing."""
        out, warns = [], []
        try:
            entries = sorted(d.iterdir())
        except (PermissionError, OSError) as e:
            return out, [f"{self._rel(d)}: unreadable — {e}"]
        for e in entries:
            if e.is_symlink():
                # resolve and refuse escape — a symlink inside the root that
                # points outside is disclosed, never followed
                try:
                    rp = e.resolve()
                except OSError:
                    warns.append(f"{self._rel(e)}: broken symlink — skipped")
                    continue
                if self.root.is_dir() and not rp.is_relative_to(self.root):
                    warns.append(f"{self._rel(e)}: symlink escapes the root — skipped")
                    continue
                e = rp
            if e.is_dir():
                if e.name in SKIP_DIRS or e.name.startswith(".git"):
                    continue
                out.append(f"dir:{e}")
            elif e.is_file():
                if is_secret_name(e.name):
                    warns.append(f"{self._rel(e)}: looks like a secret — skipped")
                    continue
                out.append(f"file:{e}")
        return out, warns

    def _page(self, f: Path) -> tuple[dict, list]:
        meta, data, _ = self._read(f)
        chunks, warns = extract_file_chunks(f.name, data,
                                            **self._chunk_kwargs())
        meta["warnings"] = warns
        return meta, chunks

    def _read(self, f: Path) -> tuple[dict, bytes, str]:
        """Fetch worker: stat + read + hash — extraction stays on the
        main thread so unchanged files can skip it entirely."""
        try:
            st = f.stat()
            if st.st_size > MAX_FILE_BYTES:
                return ({"page_id": self._rel(f), "title": f.name,
                         "url": f"file://{f}",
                         "version": f"{st.st_mtime_ns}:{st.st_size}",
                         "content_hash": "",
                         "warnings": [
                             f"file too large ({st.st_size} bytes) — skipped"]},
                        b"", "")
            data = f.read_bytes()
        except OSError as e:
            raise SetupError(f"{self._rel(f)}: unreadable — {e}") from e
        h = hashlib.sha256(data).hexdigest()
        return ({"page_id": self._rel(f), "title": f.name,
                 "url": f"file://{f}",
                 "version": f"{st.st_mtime_ns}:{st.st_size}",
                 "content_hash": h, "warnings": []},
                data, h)

    def _checkpoint(self, job_id, queue, errors, seen, levels, warns,
                    keep=()) -> None:
        with self.index.lock, self.index.db:
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"seen:{job_id}", json.dumps(sorted(seen))))
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"levels:{job_id}", json.dumps(levels)))
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
        """Process the durable queue to completion — files are cheap; the
        time-budget concern that drove Confluence's per-step cap doesn't apply
        to filesystem reads, and the worker calls this unbounded anyway."""
        job = self.index.job(job_id)
        if job is None:
            raise SetupError(f"unknown job {job_id}")
        if job["state"] != "running":
            return {"job_id": job_id, "state": job["state"],
                    "remaining": len(job["queue"]), "errors": job["errors"]}

        queue: deque[str] = deque(job["queue"])
        errors: list[str] = job["errors"]
        # transient read fails keep old content — durable across steps/restarts
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
        warns_raw = self.index.meta_get(f"warns:{job_id}")
        # deliberate skips (secrets, escaping symlinks, unreadable dirs) are
        # disclosures — reported to the agent but not coverage-degrading errors
        warns: list[str] = json.loads(warns_raw) if warns_raw else []
        visited_dirs: set[str] = set()   # guards symlink/dir-graph cycles

        # per-phase wall-clock stats — the field's "why is the crawl slow"
        # answer (parity with confluence crawl.py, #244). Durable under
        # stats:{job_id} so bounded step() calls accumulate.
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

        chunk_kwargs = self._chunk_kwargs()

        def _timed_children(d: Path):
            t0 = time.monotonic()
            try:
                return self._children(d)
            finally:
                with _stats_lock:
                    stats["fetch_ms"] += (time.monotonic() - t0) * 1000

        def _read_extract(f: Path):
            """Fetch worker: read + hash + EXTRACT — extraction runs in
            the pool so it overlaps the previous buffer's embed instead
            of serialising on the main thread (#244). `chunks=None`
            marks "unchanged" — the main loop stages live rows, no work
            wasted on identical bytes."""
            t0 = time.monotonic()
            meta, data, h = self._read(f)
            with _stats_lock:
                stats["fetch_ms"] += (time.monotonic() - t0) * 1000
            if not data:
                return meta, data, [], h     # oversized — warns set
            if self.index.live_page_hash(meta["page_id"]) == h:
                return meta, data, None, h   # unchanged → copy live rows
            t0 = time.monotonic()
            chunks, warns = extract_file_chunks(f.name, data,
                                                **chunk_kwargs)
            with _stats_lock:
                stats["extract_ms"] += (time.monotonic() - t0) * 1000
            meta["warnings"] = warns
            for c in chunks:
                c.chunk_id = f"{meta['page_id']}:{h[:8]}:{c.ordinal}"
            return meta, data, chunks, h

        processed = 0
        # throttled checkpoints — full state used to be rewritten per item
        last_ckpt, last_ckpt_n = time.monotonic(), 0
        # bounded fetch pipeline — KNOWLEDGE_FETCH_CONCURRENCY workers read
        # files/dirs ahead while the main thread extracts+embeds+stages;
        # unchanged files (same content_hash) reuse their live rows
        conc = limits().fetch_concurrency
        # the pool is sized at the adaptive ceiling once; the submission
        # gate re-reads fetch_concurrency() each pass so the window
        # breathes mid-step under pressure (#184, parity #179)
        pool_n = (self.control.fetch_ceiling(conc)
                  if self.control is not None else conc)
        inflight: dict = {}             # future -> entry string
        pending: list = []              # (rel, meta, chunks, hash, lvl, entry)
        visited_submit: set[str] = set()
        # single embed/stage worker: dense embedding is the dominant serial
        # phase (~87% of a docx-corpus wall) — running it beside the
        # fetch/extract loop overlaps model time with I/O. One worker means
        # at most one flush in flight, so text+vector buffers stay bounded
        # to ~2×FLUSH_MAX_CHUNKS (#255, parity with confluence crawl)
        flush_fut = None
        flush_entries: set[str] = set()  # queue entries inside the in-flight
                                         # flush — checkpoints re-queue them
                                         # so a crash mid-flush re-crawls

        def _do_flush(buf: list, n_chunks: int) -> None:
            """Embed + stage a page buffer on the embed worker; marks `seen`
            only after rows land. Raises where the inline version returned
            False — `_join_flush` restores the bool contract."""
            vecs = sp = None
            if self.embedder is not None:
                if self.control is not None:
                    self.control.check()
                    self.control.phase("embedding"
                                       + (" (battery saver)" if
                                          self.control.battery_saver()
                                          else ""))
                texts = [f"{m['title']}\n{c.heading_path}\n{c.text}"
                         for _, m, chunks, *_ in buf for c in chunks]
                if texts:
                    tick_t0 = time.monotonic()

                    def _embed_tick(done: int, total: int,
                                    _c=self.control) -> None:
                        nonlocal tick_t0
                        """Heartbeat + progress inside a dense flush — a
                        big docx used to freeze the visible job for
                        minutes, indistinguishable from a wedged crawl."""
                        if _c is None:
                            return
                        _c.check()           # cancel + lease heartbeat
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
            t0 = time.monotonic()
            for rel, meta, chunks, h, lvl, _e in buf:
                n = len(chunks)
                self.index.upsert_page({
                    "page_id": rel, "parent_id": None,
                    "title": meta["title"], "url": meta["url"],
                    "level": lvl,
                    "version": meta["version"], "content_hash": h,
                    "warnings": meta["warnings"]}, chunks,
                    vecs[off:off + n] if vecs is not None else None,
                    sp[off:off + n] if sp is not None else None,
                    job_id=job_id)
                off += n
            stats["store_ms"] += (time.monotonic() - t0) * 1000
            stats["pages"] += len(buf)
            stats["chunks"] += n_chunks
            seen.update(rel for rel, *_ in buf)
            if self.control is not None:
                self.control.page_done(len(buf))
                self.control.phase("fetching")

        def _join_flush() -> bool:
            """False when the in-flight flush hit StaleJobError; other
            failures (cancel, embed) propagate like the inline version."""
            nonlocal flush_fut, flush_entries
            if flush_fut is None:
                return True
            try:
                flush_fut.result()
            except StaleJobError:
                return False
            finally:
                flush_fut = None
                flush_entries = set()
            return True

        def _flush_pending() -> bool:
            """Join the previous flush (back-pressure: ≤1 in flight), then
            hand the next ≤FLUSH_MAX_CHUNKS buffer to the embed worker."""
            nonlocal pending, flush_fut, flush_entries
            if not _join_flush():
                return False
            if not pending:
                return True
            # cap each flush at ~FLUSH_MAX_CHUNKS: pages are popped from
            # the head until the cap is hit, the rest stay pending —
            # bounds a single flush's embed time and heartbeats the job
            # through a big docx (#244, parity with confluence crawl)
            buf, n_chunks = [], 0
            while pending and (n_chunks < FLUSH_MAX_CHUNKS or not buf):
                pending_item = pending.pop(0)
                buf.append(pending_item)
                n_chunks += len(pending_item[2])
            flush_entries = {e for *_, e in buf}
            flush_fut = epool.submit(_do_flush, buf, n_chunks)
            return True

        with ThreadPoolExecutor(max_workers=pool_n,
                                thread_name_prefix="crawl-fetch") as pool, \
                ThreadPoolExecutor(max_workers=1,
                                   thread_name_prefix="crawl-embed") as epool:
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
                        job_id, list(queue) + sorted(inflight.values())
                        + sorted(flush_entries),
                        errors, seen, levels, warns, keep_failed)
                    _save_stats()
                    last_ckpt, last_ckpt_n = time.monotonic(), processed
                cur = (self.control.fetch_concurrency(conc)
                       if self.control is not None else conc)
                while queue and len(inflight) < cur:
                    entry = queue.popleft()
                    # "dir:<path>" / "file:<path>" — split on the PREFIX,
                    # not ':' (Windows paths carry their own drive colon)
                    is_dir = entry.startswith("dir:")
                    path_s = entry[4:] if is_dir else entry[5:]
                    if path_s in visited_submit or path_s in seen:
                        continue
                    visited_submit.add(path_s)
                    path = Path(path_s)
                    if is_dir:
                        real = str(path.resolve())
                        if real in visited_dirs:
                            continue  # symlink/loop — dirs aren't pages
                        visited_dirs.add(real)
                        inflight[pool.submit(_timed_children, path)] = entry
                    else:
                        inflight[pool.submit(_read_extract, path)] = entry
                if not inflight:
                    break
                t_wait = time.monotonic()
                done, _ = wait(tuple(inflight),
                               return_when=FIRST_COMPLETED)
                stats["wait_ms"] += (time.monotonic() - t_wait) * 1000
                for fut in done:
                    entry = inflight.pop(fut)
                    processed += 1
                    is_dir = entry.startswith("dir:")
                    path_s = entry[4:] if is_dir else entry[5:]
                    try:
                        result = fut.result()
                    except SetupError as e:
                        errors.append(str(e))
                        if not is_dir:
                            keep_failed.add(self._rel(Path(path_s)))
                        continue
                    except Exception as e:  # noqa: BLE001 — transient I/O
                        errors.append(f"{path_s}: {type(e).__name__}: {e}")
                        if not is_dir:
                            keep_failed.add(self._rel(Path(path_s)))
                        continue
                    if is_dir:
                        kids, warns2 = result
                        warns.extend(warns2)
                        if not kids and warns2:
                            # listing may be incomplete (unreadable dir) —
                            # not proof of absence: keep prior files under it
                            rel = self._rel(Path(path_s))
                            keep_failed.update(
                                p for p in self.index.active_pages()
                                if p == rel or p.startswith(rel + "/"))
                        queue.extend(
                            k for k in kids
                            if (k[4:] if k.startswith("dir:")
                                else k[5:]) not in seen)
                        continue
                    meta, data, chunks, content_hash = result
                    rel = meta["page_id"]
                    if rel in seen:
                        continue
                    if chunks is None:
                        # worker confirmed unchanged — stage live rows;
                        # identical bytes never re-embed
                        if self.index.copy_live_to_stage(rel, job_id):
                            seen.add(rel)
                            if self.control is not None:
                                self.control.page_done(1)
                            continue
                        # hash matched but stage copy failed (live row
                        # vanished mid-crawl) — extract on demand
                        chunks, warns2 = extract_file_chunks(
                            Path(path_s).name, data, **chunk_kwargs)
                        meta["warnings"] = warns2
                        for c in chunks:
                            c.chunk_id = (f"{rel}:{content_hash[:8]}:"
                                          f"{c.ordinal}")
                    pending.append((rel, meta, chunks, content_hash,
                                    levels.get(rel, path_s.count("/")),
                                    entry))
                    if (sum(len(c) for _, _, c, *_ in pending) >= 64
                            and not _flush_pending()):
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
            # drain the buffer the fetch loop left behind, then join the
            # last in-flight flush — publish must not run over half-staged
            # pages (inside the `with`: the embed pool is still open)
            while pending:
                if not _flush_pending():
                    return {"job_id": job_id, "state": "abandoned",
                            "remaining": len(queue), "pages_seen": len(seen),
                            "errors": errors, "warnings": warns}
            if not _join_flush():
                return {"job_id": job_id, "state": "abandoned",
                        "remaining": len(queue), "pages_seen": len(seen),
                        "errors": errors, "warnings": warns}

        # persist final seen/levels/warns — the throttled in-loop
        # checkpoint may be several items behind the last processed one
        self._checkpoint(job_id,
                         list(queue) + sorted(inflight.values())
                         + sorted(flush_entries),
                         errors, seen, levels, warns, keep_failed)
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
            t_pub = time.monotonic()
            pub = self.index.publish(seen | keep_failed, job_id=job_id)
            stats["publish_ms"] = stats.get("publish_ms", 0.0) + \
                (time.monotonic() - t_pub) * 1000
            # auditable outcome: what the refresh added vs tombstoned vs
            # carried forward — stored so status/last_crawl can report it
            self.index.meta_set(f"publish:{job_id}", json.dumps(pub))
            self.index.set_model_id(_MODEL_ID)
            _save_stats()
        self.index.save_job(job_id, "done" if not queue else "running",
                            list(queue) + sorted(flush_entries), errors)
        return {"job_id": job_id,
                "state": "done" if not queue else "running",
                "remaining": len(queue), "pages_seen": len(seen),
                "errors": errors, "warnings": warns}
