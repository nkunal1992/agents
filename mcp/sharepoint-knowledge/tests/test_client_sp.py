"""Client: GET-only enforcement, path bounds, no-Bearer downloads."""
from __future__ import annotations

import pytest
from sharepoint_knowledge.client_sp import GRAPH_RULES, download_file, make_client

from confluence_knowledge.client import SecurityException


def test_write_methods_rejected():
    c = make_client("http://127.0.0.1:1/v1.0", "tok")
    for m in ("POST", "PUT", "DELETE", "PATCH"):
        with pytest.raises(SecurityException, match="read-only"):
            c.request(m, "sites/x")


def test_non_allowlisted_route_rejected():
    c = make_client("http://127.0.0.1:1/v1.0", "tok")
    with pytest.raises(SecurityException):
        c.get("admin/serviceAnnouncement/messages")


def test_allowlisted_route_shapes_pass(monkeypatch):
    c = make_client("http://127.0.0.1:1/v1.0", "tok")
    for p in ("sites/t.sharepoint.com:/sites/x",
              "sites/id1/lists", "sites/id1/lists/l1/items",
              "sites/id1/lists/l1/items/i1", "sites/id1/drives",
              "sites/id1/drive/root:/a/b.md",
              "drives/d1/root/children", "drives/d1/items/i1",
              "drives/d1/items/i1/children", "me"):
        c._validate_path(p)  # raises SecurityException on rejection


def test_path_escape_rejected():
    c = make_client("http://127.0.0.1:1/v1.0", "tok")
    with pytest.raises(SecurityException):
        c.get("../secret")


def test_download_no_auth_header(monkeypatch):
    """downloadUrl fetches must NOT carry the Bearer token — the URL is
    self-authenticating and the token must never leave the API host."""
    sent = {}

    class FakeStream:
        def __init__(self, *a, **k):
            self.status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self, n):
            yield b"data"

    class FakeClient:
        def __init__(self, *a, **k):
            sent["headers"] = k.get("headers", {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, *a, **k):
            return FakeStream()

    import httpx
    monkeypatch.setattr(httpx, "Client", FakeClient)
    assert download_file("https://t.sharepoint.com/dl/x") == b"data"
    assert "Authorization" not in sent["headers"]


def test_download_http_rejected():
    with pytest.raises(SecurityException):
        download_file("https+http://not-https.example.com/x")
    with pytest.raises(SecurityException):
        download_file("ftp://x/y")


def test_rules_cover_all_crawl_paths():
    """The allowlist must contain every path the crawler can emit."""
    needed = [
        "sites/host.example.com:/sites/team",
        "sites/SITEID/lists", "sites/SITEID/lists/LID/items",
        "sites/SITEID/lists/LID/items/IID", "sites/SITEID/drives",
        "sites/SITEID/drive", "sites/SITEID/drive/root:/a/b.md",
        "drives/DRVID/root/children", "drives/DRVID/items/IID",
        "drives/DRVID/items/IID/children", "me",
    ]
    for p in needed:
        assert any(r.pattern.match(p) for r in GRAPH_RULES), \
            f"allowlist missing route shape: {p}"
