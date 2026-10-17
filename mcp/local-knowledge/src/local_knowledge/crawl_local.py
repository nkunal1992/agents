"""Durable, resumable filesystem crawl — one path (file or folder) → index.

Same durable-queue/checkpoint contract as the Confluence crawler: progress
survives interruption, the background worker runs to completion, and only a
completed crawl publishes (a partial refresh never clobbers a good snapshot).
Reads only — never writes, follows no symlinks outside the root.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from confluence_knowledge.extract_files import extract_file_chunks, is_secret_name
from confluence_knowledge.index import Index
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


from confluence_knowledge.retrieval import MODEL_MARKER as _MODEL_ID


class LocalCrawler:
    def __init__(self, index: Index, root: Path, embedder=None, sparse=None):
        self.index = index
        self.root = root      # realpath — everything indexed must live under it
        self.embedder = embedder
        self.sparse = sparse

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
        try:
            st = f.stat()
            if st.st_size > MAX_FILE_BYTES:
                return ({"page_id": self._rel(f), "title": f.name,
                         "url": f"file://{f}", "version": f"{st.st_mtime_ns}:{st.st_size}",
                         "content_hash": "",
                         "warnings": [f"file too large ({st.st_size} bytes) — skipped"]},
                        [])
            data = f.read_bytes()
        except OSError as e:
            raise SetupError(f"{self._rel(f)}: unreadable — {e}") from e
        chunks, warns = extract_file_chunks(f.name, data)
        content_hash = hashlib.sha256(data).hexdigest()
        return ({"page_id": self._rel(f), "title": f.name,
                 "url": f"file://{f}",
                 "version": f"{st.st_mtime_ns}:{st.st_size}",
                 "content_hash": content_hash,
                 "warnings": warns},
                chunks)

    def _checkpoint(self, job_id, queue, errors, seen, levels, warns) -> None:
        with self.index.lock, self.index.db:
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"seen:{job_id}", json.dumps(sorted(seen))))
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"levels:{job_id}", json.dumps(levels)))
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                                  (f"warns:{job_id}", json.dumps(warns)))
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

        queue: list[str] = job["queue"]
        errors: list[str] = job["errors"]
        keep_failed: set[str] = set()  # transient read fails keep old content
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
        while queue:
            if len(seen) >= MAX_FILES:
                errors.append(f"file cap reached ({MAX_FILES}) — remaining skipped")
                queue.clear()
                break
            if processed:
                self._checkpoint(job_id, queue, errors, seen, levels, warns)
            entry = queue.pop(0)
            # entries are "dir:<path>" / "file:<path>" — split on the PREFIX,
            # not ':' (Windows paths contain their own drive colon)
            is_dir_entry = entry.startswith("dir:")
            path_s = entry[4:] if is_dir_entry else entry[5:]
            path = Path(path_s)
            if path_s in seen:
                continue
            processed += 1
            if is_dir_entry:
                real = str(path.resolve())
                if real in visited_dirs:
                    continue  # symlink/loop — directories aren't pages
                visited_dirs.add(real)
                kids, warns2 = self._children(path)
                warns.extend(warns2)
                queue.extend(k for k in kids
                             if (k[4:] if k.startswith("dir:") else k[5:]) not in seen)
                continue
            try:
                page, chunks = self._page(path)
            except SetupError as e:
                errors.append(str(e))
                keep_failed.add(self._rel(path))
                continue
            rel = page["page_id"]
            if rel in seen:
                continue
            for c in chunks:
                c.chunk_id = f"{rel}:{page['content_hash'][:8]}:{c.ordinal}"
            vecs, sparse_vecs = None, None
            if self.embedder is not None and chunks:
                texts = [f"{c.heading_path}\n{c.text}" for c in chunks]
                vecs = self.embedder.embed(texts)
                if self.sparse is not None:
                    sparse_vecs = self.sparse.embed(texts)
            self.index.upsert_page({
                "page_id": rel, "parent_id": None, "title": page["title"],
                "url": page["url"], "level": levels.get(rel, path_s.count("/")),
                "version": page["version"], "content_hash": page["content_hash"],
                "warnings": page["warnings"]}, chunks, vecs, sparse_vecs)
            seen.add(rel)

        # persist final seen/levels/warns too — the in-loop checkpoint runs
        # before each item is processed, so the last item would undercount
        self._checkpoint(job_id, queue, errors, seen, levels, warns)
        self.index.publish(seen | keep_failed)
        self.index.set_model_id(_MODEL_ID)
        self.index.save_job(job_id, "done" if not queue else "running",
                            queue, errors)
        return {"job_id": job_id,
                "state": "done" if not queue else "running",
                "remaining": len(queue), "pages_seen": len(seen),
                "errors": errors, "warnings": warns}
