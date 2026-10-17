"""Fake Microsoft Graph server for the SharePoint e2e — token-gated,
localhost-only, serving the exact routes the crawler hits.

Routes (all GET):
  /v1.0/sites/<host>:<path>         → site descriptor
  /v1.0/sites/<site>/lists          → [Site Pages]
  /v1.0/sites/<site>/drives         → [Documents]
  /v1.0/sites/<site>/lists/<l>/items → page stubs
  /v1.0/sites/<site>/lists/<l>/items/<i> → page w/ CanvasContent1
  /v1.0/drives/<d>/root/children    → folder + file stubs
  /v1.0/drives/<d>/items/<i>/children → file stubs
  /v1.0/drives/<d>/items/<i>        → metadata + downloadUrl
  /v1.0/me                          → identity
  /download/<name>                  → file bytes (NO auth — like downloadUrl)
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

TOKEN = "e2e-sp-token-canary"
DELAY = 0.3

SITE = "site-guid-1"
LIST = "list-guid-1"
DRIVE = "drive-guid-1"
HOST = "localhost"  # the fake pretends to be t.sharepoint.com via base override

PAGES = {
    "pg1": ("Release Handbook",
            ("<h1>Approvals</h1><p>Every production release requires sign-off "
             "from the Change Advisory Board.</p>")),
    "pg2": ("Deploy Windows",
            ("<h2>Schedule</h2><p>Production deploys happen only on Thursdays "
             "after 18:00.</p>")),
}

FILES = {
    "runbook.md": (
        b"# Runbook\n\nDeploys happen only on Thursdays after 18:00.\n"),
    "secrets.env": b"API_KEY=never-indexed",
}

ITEMS = {
    "f1": {"id": "f1", "name": "policies", "folder": {}},
    "f2": {"id": "f2", "name": "runbook.md", "size": 80},
    "f3": {"id": "f3", "name": "secrets.env", "size": 40},
}
CHILDREN = {"root": ["f1", "f2"], "f1": ["f3"], "f2": [], "f3": []}


def make_handler(hits: list[str], noauth_hits: list[str], port_ref: list[int]):
    class FakeGraph(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _file(self, data):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = unquote(urlparse(self.path).path).rstrip("/")
            if path.startswith("/download/"):
                # pre-authenticated URL — intentionally no auth check
                noauth_hits.append(path)
                name = path.split("/")[-1]
                if name in FILES:
                    time.sleep(DELAY)
                    return self._file(FILES[name])
                return self._json(404, {})
            hits.append(path)
            time.sleep(DELAY)
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._json(401, {})
            if path == "/v1.0/me":
                return self._json(200, {"userPrincipalName": "e2e@t.test"})
            if path.startswith("/v1.0/sites/") and ":" in path.split("/v1.0/sites/")[1]:
                # sites/<host>:<path> — site resolution
                return self._json(200, {
                    "id": SITE,
                    "webUrl": f"http://127.0.0.1:{port_ref[0]}/sites/team",
                    "displayName": "Team Site"})
            parts = [p for p in path.split("/") if p]
            if parts[:3] == ["v1.0", "sites", SITE]:
                rest = parts[3:]
                if rest == ["lists"]:
                    return self._json(200, {"value": [
                        {"id": LIST, "displayName": "Site Pages",
                         "list": {"template": "webPageLibrary"}}]})
                if rest == ["drives"]:
                    return self._json(200, {"value": [
                        {"id": DRIVE, "name": "Documents"}]})
                if rest == ["lists", LIST, "items"]:
                    return self._json(200, {"value": [
                        {"id": p} for p in PAGES]})
                if rest[:3] == ["lists", LIST, "items"] and len(rest) == 4:
                    pid = rest[3]
                    if pid in PAGES:
                        title, canvas = PAGES[pid]
                        return self._json(200, {
                            "id": pid,
                            "webUrl": f"http://127.0.0.1:{port_ref[0]}/pg/{pid}",
                            "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                            "fields": {"Title": title,
                                       "CanvasContent1": canvas}})
                if rest == ["drive"]:
                    return self._json(200, {"id": DRIVE})
            if parts[:2] == ["v1.0", "drives"]:
                rest = parts[3:]
                if rest == ["root", "children"]:
                    return self._json(200, {"value": [
                        ITEMS[i] for i in CHILDREN["root"]]})
                if rest[0] == "items" and len(rest) == 3 \
                        and rest[2] == "children":
                    iid = rest[1]
                    return self._json(200, {"value": [
                        ITEMS[i] for i in CHILDREN.get(iid, [])]})
                if rest[:1] == ["items"] and len(rest) == 2:
                    iid = rest[1]
                    if iid in ITEMS:
                        d = dict(ITEMS[iid])
                        if "folder" not in d:
                            d.update({
                                "webUrl": f"http://127.0.0.1:{port_ref[0]}/f/{iid}",
                                "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                                "@microsoft.graph.downloadUrl":
                                    f"http://127.0.0.1:{port_ref[0]}/download/{d['name']}"})
                        return self._json(200, d)
            return self._json(404, {})

    return FakeGraph


def serve() -> tuple[ThreadingHTTPServer, str, list[str], list[str]]:
    """Returns (server, graph_base_url, hits, noauth_hits)."""
    hits: list[str] = []
    noauth_hits: list[str] = []
    port_ref: list[int] = [0]
    srv = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(hits, noauth_hits, port_ref))
    port_ref[0] = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}/v1.0", hits, noauth_hits
