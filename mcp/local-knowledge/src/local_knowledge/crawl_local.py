"""Durable, resumable filesystem crawl — one path (file or folder) → index.

Same durable-queue/checkpoint contract as the Confluence crawler: progress
survives interruption, the background worker runs to completion, and only a
completed crawl publishes (a partial refresh never clobbers a good snapshot).
Reads only — never writes, follows no symlinks outside the root.
"""

from __future__ import annotations

import hashlib
import json
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
    def __init__(self, index: Index, root: Path, embedder=None, sparse=None):
        self.index = index
        self.root = root      # realpath — everything indexed must live under it
        self.embedder = embedder
        self.sparse = sparse

    def _chunk_kwargs(self) -> dict:
        measure = getattr(self.embedder, "count_tokens", None)
        return {"measure": measure, "budget": CHUNK_TOKENS} if measure else {}

    def start(self) -> str:
        job_id = uuid.uuid4().hex[:12]
        self.index.abandon_running_jobs()
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

        processed = 0
        # throttled checkpoints — full state used to be rewritten per item
        last_ckpt, last_ckpt_n = time.monotonic(), 0
        # bounded fetch pipeline — KNOWLEDGE_FETCH_CONCURRENCY workers read
        # files/dirs ahead while the main thread extracts+embeds+stages;
        # unchanged files (same content_hash) reuse their live rows
        conc = limits().fetch_concurrency
        inflight: dict = {}             # future -> entry string
        pending: list = []              # (rel, meta, chunks, hash, lvl)
        visited_submit: set[str] = set()

        def _flush_pending() -> bool:
            """Stage buffered pages; False on StaleJobError. `seen` updates
            only after rows land — buffered-but-unstaged files must be
            re-crawled on resume."""
            nonlocal pending
            if not pending:
                return True
            buf, pending = pending, []
            vecs = sp = None
            if self.embedder is not None:
                texts = [f"{m['title']}\n{c.heading_path}\n{c.text}"
                         for _, m, chunks, *_ in buf for c in chunks]
                if texts:
                    vecs = self.embedder.embed(texts)
                    if self.sparse is not None:
                        sp = self.sparse.embed(texts)
            off = 0
            for rel, meta, chunks, h, lvl in buf:
                n = len(chunks)
                try:
                    self.index.upsert_page({
                        "page_id": rel, "parent_id": None,
                        "title": meta["title"], "url": meta["url"],
                        "level": lvl,
                        "version": meta["version"], "content_hash": h,
                        "warnings": meta["warnings"]}, chunks,
                        vecs[off:off + n] if vecs is not None else None,
                        sp[off:off + n] if sp is not None else None,
                        job_id=job_id)
                except StaleJobError:
                    return False
                off += n
            seen.update(rel for rel, *_ in buf)
            return True

        with ThreadPoolExecutor(max_workers=conc,
                                thread_name_prefix="crawl-fetch") as pool:
            while queue or inflight:
                if len(seen) >= MAX_FILES:
                    errors.append(
                        f"file cap reached ({MAX_FILES}) — remaining skipped")
                    queue.clear()
                    inflight.clear()
                    break
                if processed and (time.monotonic() - last_ckpt >= 5.0
                                  or processed - last_ckpt_n >= 50):
                    self._checkpoint(
                        job_id, list(queue) + sorted(inflight.values()),
                        errors, seen, levels, warns, keep_failed)
                    last_ckpt, last_ckpt_n = time.monotonic(), processed
                while queue and len(inflight) < conc:
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
                        inflight[pool.submit(self._children, path)] = entry
                    else:
                        inflight[pool.submit(self._read, path)] = entry
                if not inflight:
                    break
                done, _ = wait(tuple(inflight),
                               return_when=FIRST_COMPLETED)
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
                    meta, data, content_hash = result
                    rel = meta["page_id"]
                    if rel in seen:
                        continue
                    # unchanged file: reuse its live rows instead of
                    # re-extracting + re-embedding identical bytes
                    if (self.index.live_page_hash(rel) == content_hash
                            and self.index.copy_live_to_stage(rel, job_id)):
                        seen.add(rel)
                        continue
                    if data:
                        chunks, warns2 = extract_file_chunks(
                            Path(path_s).name, data, **self._chunk_kwargs())
                        meta["warnings"] = warns2
                        for c in chunks:
                            c.chunk_id = (f"{rel}:{content_hash[:8]}:"
                                          f"{c.ordinal}")
                    else:
                        chunks = []   # oversized — warnings already set
                    pending.append((rel, meta, chunks, content_hash,
                                    levels.get(rel, path_s.count("/"))))
                    if (sum(len(c) for _, _, c, _, _ in pending) >= 64
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
        if not _flush_pending():
            return {"job_id": job_id, "state": "abandoned",
                    "remaining": len(queue), "pages_seen": len(seen),
                    "errors": errors, "warnings": warns}

        # persist final seen/levels/warns — the throttled in-loop
        # checkpoint may be several items behind the last processed one
        self._checkpoint(job_id,
                         list(queue) + sorted(inflight.values()),
                         errors, seen, levels, warns, keep_failed)
        # fence: an abandoned/superseded job must never publish or resurrect
        current = self.index.job(job_id)
        if current is None or current["state"] != "running":
            return {"job_id": job_id,
                    "state": current["state"] if current else "abandoned",
                    "remaining": len(queue), "pages_seen": len(seen),
                    "errors": errors, "warnings": warns}
        if not queue:
            self.index.publish(seen | keep_failed, job_id=job_id)
            self.index.set_model_id(_MODEL_ID)
        self.index.save_job(job_id, "done" if not queue else "running",
                            list(queue), errors)
        return {"job_id": job_id,
                "state": "done" if not queue else "running",
                "remaining": len(queue), "pages_seen": len(seen),
                "errors": errors, "warnings": warns}
