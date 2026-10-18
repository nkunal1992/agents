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
import uuid
from urllib.parse import urlparse

from confluence_knowledge.client import AccessError, ConfluenceClient
from confluence_knowledge.extract import extract_chunks
from confluence_knowledge.extract_files import extract_file_chunks, is_secret_name
from confluence_knowledge.index import Index
from confluence_knowledge.settings import SetupError

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_FILES = 20_000

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
    client's origin+base by _validate_path, so pagination stays in scope."""
    while True:
        r = client.get(path, params)
        d = r.json()
        yield from d.get("value", [])
        nxt = d.get("@odata.nextLink")
        if not nxt:
            return
        path, params = nxt, None


class SPCrawler:
    def __init__(self, client: ConfluenceClient, index: Index, embedder=None,
                 downloader=None, sparse=None):
        self.client = client
        self.index = index
        self.embedder = embedder
        self.sparse = sparse
        self.downloader = downloader  # injected for tests; real one in server

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

    def _page_from_list_item(self, site_id: str, list_id: str,
                             item_id: str) -> tuple[dict, list, list]:
        d = self.client.get(
            f"sites/{site_id}/lists/{list_id}/items/{item_id}",
            {"$expand": "fields"}).json()
        fields = d.get("fields", {})
        title = fields.get("Title") or d.get("name") or item_id
        canvas = fields.get("CanvasContent1") or fields.get("WikiField") or ""
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
            chunks, warns = extract_chunks(html)
        else:
            warns.append("no canvas content — page may be empty or classic")
        page = {"page_id": f"page:{item_id}", "title": title,
                "url": (d.get("webUrl")
                        or (fields.get("_dlc_DocIdUrl") or {}).get("Url", "")),
                "version": str(d.get("lastModifiedDateTime") or ""),
                "content_hash": hashlib.sha256(canvas.encode()).hexdigest(),
                "warnings": warns}
        return page, chunks, warns

    def _page_from_item(self, drive_id: str, item_id: str,
                        downloader) -> tuple[dict, list, list]:
        d = self.client.get(
            f"drives/{drive_id}/items/{item_id}",
            {"$select": "id,name,size,webUrl,lastModifiedDateTime,"
                        "@microsoft.graph.downloadUrl"}).json()
        name = d.get("name", item_id)
        warns: list[str] = []
        if is_secret_name(name):
            # disclose-and-skip — same posture as local files: the file is
            # never a page in the index, only a warning on the job
            return None, [], [f"{name}: looks like a secret — skipped"]
        dl_url = d.get("@microsoft.graph.downloadUrl")
        if not dl_url or (d.get("size") or 0) > MAX_FILE_BYTES:
            why = ("no download URL" if not dl_url
                   else f"too large ({d['size']} bytes)")
            return None, [], [f"{name}: {why} — skipped"]
        data = downloader(dl_url)
        chunks, warns = extract_file_chunks(name, data)
        return {"page_id": f"file:{item_id}", "title": name,
                "url": d.get("webUrl", ""),
                "version": str(d.get("lastModifiedDateTime", "")),
                "content_hash": hashlib.sha256(data).hexdigest(),
                "warnings": warns}, chunks, warns

    # ---- the crawl ----

    def start(self, root_entry: str) -> str:
        job_id = uuid.uuid4().hex[:12]
        self.index.abandon_running_jobs()
        self.index.create_job(job_id, root_entry)
        self.index.save_job(job_id, "running", [root_entry], [])
        return job_id

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
        job = self.index.job(job_id)
        if job is None:
            raise AccessError(f"unknown job {job_id}")
        if job["state"] != "running":
            return {"job_id": job_id, "state": job["state"],
                    "remaining": len(job["queue"]), "errors": job["errors"]}

        queue: list[str] = job["queue"]
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
        warns_raw = self.index.meta_get(f"warns:{job_id}")
        warns: list[str] = json.loads(warns_raw) if warns_raw else []

        processed = 0
        while queue:
            if len(seen) >= MAX_FILES:
                errors.append(f"file cap reached ({MAX_FILES}) — remaining skipped")
                queue.clear()
                break
            if processed:
                self._checkpoint(job_id, queue, errors, seen, levels, warns,
                                 keep_failed)
            entry = queue.pop(0)
            processed += 1
            try:
                if entry.startswith("site:"):
                    kids, w = self._expand_site(entry[5:])
                    warns.extend(w)
                    queue.extend(k for k in kids if k not in seen)
                    continue
                if entry.startswith("list:"):
                    _, site_id, list_id = entry.split(":", 2)
                    for it in _paged(
                            self.client,
                            f"sites/{site_id}/lists/{list_id}/items"):
                        queue.append(f"page:{site_id}:{list_id}:{it['id']}")
                    continue
                if entry.startswith("drive:"):
                    queue.extend(self._expand_drive(entry[6:]))
                    continue
                if entry.startswith("page:"):
                    _, site_id, list_id, item_id = entry.split(":", 3)
                    page, chunks, w = self._page_from_list_item(
                        site_id, list_id, item_id)
                    warns.extend(w)
                elif entry.startswith("item:"):
                    _, drive_id, item_id = entry.split(":", 2)
                    meta = self.client.get(
                        f"drives/{drive_id}/items/{item_id}").json()
                    if meta.get("folder") is not None:
                        queue.extend(self._expand_item(drive_id, item_id))
                        continue
                    page, chunks, w = self._page_from_item(
                        drive_id, item_id, self.downloader)
                    warns.extend(w)
                    if page is None:
                        continue
                else:
                    errors.append(f"unknown queue entry {entry}")
                    continue
            except AccessError as e:
                errors.append(f"{entry}: {e}")
                if not any(m in str(e) for m in
                           ("401", "403", "404", "permission", "not found")):
                    # seen stores the built page_id — page: / file: prefixed
                    last = entry.split(":")[-1]
                    keep_failed.add(
                        f"page:{last}" if entry.startswith("page:")
                        else f"file:{last}")
                continue
            pid = page["page_id"]
            if pid in seen:
                continue
            for c in chunks:
                c.chunk_id = f"{pid}:{page['content_hash'][:8]}:{c.ordinal}"
            vecs, sparse_vecs = None, None
            if self.embedder is not None and chunks:
                texts = [f"{c.heading_path}\n{c.text}" for c in chunks]
                vecs = self.embedder.embed(texts)
                if self.sparse is not None:
                    sparse_vecs = self.sparse.embed(texts)
            self.index.upsert_page({
                "page_id": pid, "parent_id": None, "title": page["title"],
                "url": page["url"], "level": levels.get(pid, 0),
                "version": page["version"],
                "content_hash": page["content_hash"],
                "warnings": page["warnings"]}, chunks, vecs, sparse_vecs,
                job_id=job_id)
            seen.add(pid)

        self._checkpoint(job_id, queue, errors, seen, levels, warns,
                         keep_failed)
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
                            queue, errors)
        return {"job_id": job_id,
                "state": "done" if not queue else "running",
                "remaining": len(queue), "pages_seen": len(seen),
                "errors": errors, "warnings": warns}
