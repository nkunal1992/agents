"""In-process fake Graph client for sharepoint tests — mirrors the shapes
the crawler consumes. For the HTTP-level fake (e2e) see
scripts/_fake_graph_server.py."""

from __future__ import annotations

SITE_ID = "site-guid-1"
LIST_ID = "list-guid-1"
DRIVE_ID = "drive-guid-1"


class Resp:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d


class FakeGraph:
    """client.get(path, params) → Resp — no network, no auth."""

    def __init__(self, routes: dict | None = None):
        self.routes = routes or {}
        self.hits: list[str] = []

    def get(self, path: str, params: dict | None = None) -> Resp:
        self.hits.append(path)
        if path in self.routes:
            return Resp(self.routes[path])
        from confluence_knowledge.client import AccessError
        raise AccessError(f"fake graph: unhandled route {path}")


def site_routes() -> dict:
    """One site → Site Pages list (2 pages) + one drive (folder+2 files)."""
    return {
        f"sites/{SITE_ID}/lists": {"value": [
            {"id": LIST_ID, "displayName": "Site Pages",
             "list": {"template": "webPageLibrary"}},
        ]},
        f"sites/{SITE_ID}/drives": {"value": [
            {"id": DRIVE_ID, "name": "Documents"},
        ]},
        f"sites/{SITE_ID}/lists/{LIST_ID}/items": {"value": [
            {"id": "pg1"}, {"id": "pg2"},
        ]},
        f"sites/{SITE_ID}/lists/{LIST_ID}/items/pg1": {
            "id": "pg1", "webUrl": "https://t.sharepoint.com/sites/x/pg1",
            "lastModifiedDateTime": "2026-01-01T00:00:00Z",
            "fields": {"Title": "Release Handbook",
                       "CanvasContent1":
                           "<h1>Approvals</h1><p>Every release needs "
                           "CAB sign-off.</p>"}},
        f"sites/{SITE_ID}/lists/{LIST_ID}/items/pg2": {
            "id": "pg2", "webUrl": "https://t.sharepoint.com/sites/x/pg2",
            "lastModifiedDateTime": "2026-01-01T00:00:00Z",
            "fields": {"Title": "Deploy Windows",
                       "CanvasContent1":
                           "<h2>Schedule</h2><p>Production deploys happen "
                           "only on Thursdays after 18:00.</p>"}},
        f"drives/{DRIVE_ID}/root/children": {"value": [
            {"id": "f1", "name": "policies", "folder": {}},
            {"id": "f2", "name": "runbook.md"},
        ]},
        f"drives/{DRIVE_ID}/items/f1/children": {"value": [
            {"id": "f3", "name": "secrets.env"},
        ]},
        f"drives/{DRIVE_ID}/items/f1": {
            "id": "f1", "name": "policies", "folder": {}},
        f"drives/{DRIVE_ID}/items/f2": {
            "id": "f2", "name": "runbook.md", "size": 80,
            "webUrl": "https://t.sharepoint.com/sites/x/runbook.md",
            "lastModifiedDateTime": "2026-01-01T00:00:00Z",
            "@microsoft.graph.downloadUrl":
                "https://t.sharepoint.com/dl/runbook.md"},
        f"drives/{DRIVE_ID}/items/f3": {
            "id": "f3", "name": "secrets.env", "size": 40,
            "webUrl": "https://t.sharepoint.com/sites/x/secrets.env",
            "lastModifiedDateTime": "2026-01-01T00:00:00Z",
            "@microsoft.graph.downloadUrl":
                "https://t.sharepoint.com/dl/secrets.env"},
        f"drives/{DRIVE_ID}/items/f2?x": None,  # unused
        "me": {"userPrincipalName": "user@t.test"},
    }


BLOBS = {
    "https://t.sharepoint.com/dl/runbook.md":
        b"# Runbook\n\nProduction deploys happen only on Thursdays "
        b"after 18:00.\n",
    "https://t.sharepoint.com/dl/secrets.env":
        b"API_KEY=never-indexed",
}
