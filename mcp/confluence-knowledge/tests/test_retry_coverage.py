"""#175 + #170: transient 5xx/timeouts retried; denied parents don't drop
their children-listing."""


import httpx
import numpy as np
import pytest

from confluence_knowledge.client import RULES_DC, AccessError, ConfluenceClient
from confluence_knowledge.crawl import Crawler
from confluence_knowledge.index import Index


def _client(handler):
    return ConfluenceClient("https://dc", {}, RULES_DC,
                            session=httpx.Client(
                                transport=httpx.MockTransport(handler)))


def _page(pid, ancestors=None):
    return {"id": pid, "title": f"Page {pid}",
            "body": {"storage": {"value": f"<p>content {pid}</p>"}},
            "version": {"number": 1},
            "ancestors": [{"id": a} for a in (ancestors or [])],
            "_links": {"webui": f"/p/{pid}", "base": "https://dc"}}


# ---------------- client-level retry (#175) ----------------


def test_5xx_retried_until_success():
    calls = []

    def h(req):
        calls.append(req.url.path)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    resp = _client(h).get("rest/api/content/1")
    assert resp.status_code == 200 and len(calls) == 3


def test_transport_error_retried():
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) < 2:
            raise httpx.ConnectTimeout("timed out", request=req)
        return httpx.Response(200, json={"ok": True})

    resp = _client(h).get("rest/api/content/1")
    assert resp.status_code == 200 and len(calls) == 2


def test_5xx_exhausted_raises_access_error():
    def h(req):
        return httpx.Response(502)

    with pytest.raises(AccessError) as e:
        _client(h).get("rest/api/content/1")
    assert "502" in str(e.value)


def test_4xx_never_retried():
    calls = []

    def h(req):
        calls.append(1)
        return httpx.Response(403)

    with pytest.raises(AccessError):
        _client(h).get("rest/api/content/1")
    assert len(calls) == 1  # 403 is definitive — no retry


def test_retry_after_honored_for_5xx():
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503,
                                  headers={"Retry-After": "0.5"})
        return httpx.Response(200, json={"ok": True})

    import time
    t0 = time.monotonic()
    _client(h).get("rest/api/content/1")
    assert time.monotonic() - t0 >= 0.5


# ---------------- crawl-level coverage (#170 + #175) ----------------


class _Embed:
    def embed(self, texts):
        return np.ones((len(texts), 4), dtype=np.float32)


def _denied_tree_client(forbidden_children_listing=False):
    """root 1 → denied parent 2 → viewable child 3."""
    pages = {"1": _page("1"), "2": _page("2", ["1"]), "3": _page("3", ["1", "2"])}
    children = {"1": [{"id": "2"}], "2": [{"id": "3"}], "3": []}

    def h(req):
        path = req.url.path
        if "/child/page" in path:
            pid = path.split("/content/")[1].split("/")[0]
            if forbidden_children_listing and pid == "2":
                return httpx.Response(403)
            return httpx.Response(200, json={
                "results": children.get(pid, []),
                "size": len(children.get(pid, [])), "limit": 200})
        pid = path.split("/content/")[1].rstrip("/")
        if pid == "2":
            return httpx.Response(403)
        return httpx.Response(200, json=pages[pid])

    return _client(h)


def test_children_of_denied_parent_are_crawled(tmp_path):
    idx = Index(tmp_path / "s.db")
    c = Crawler(_denied_tree_client(), idx, "datacenter")
    c._embedder = _Embed()
    job = c.start("1")
    r = c.step(job, batch=None)
    assert r["state"] == "done"
    # child 3 discovered + indexed even though parent 2's body was denied
    titles = {row[0] for row in idx.db.execute("select title from pages")}
    assert "Page 3" in titles and "Page 1" in titles
    assert "Page 2" not in titles          # the denied body itself stays out
    assert any("2" in e and "403" in e for e in r["errors"])


def test_children_listing_5xx_retried_keeps_subtree(tmp_path):
    """#175: a transient 502 on /child/page must not lose the subtree."""
    calls = {"n": 0}
    pages = {"1": _page("1"), "2": _page("2", ["1"])}
    children = {"1": [{"id": "2"}], "2": []}

    def h(req):
        path = req.url.path
        if "/child/page" in path:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(502)   # first listing flakes once
            pid = path.split("/content/")[1].split("/")[0]
            return httpx.Response(200, json={
                "results": children.get(pid, []),
                "size": len(children.get(pid, [])), "limit": 200})
        pid = path.split("/content/")[1].rstrip("/")
        return httpx.Response(200, json=pages[pid])

    idx = Index(tmp_path / "s.db")
    c = Crawler(_client(h), idx, "datacenter")
    c._embedder = _Embed()
    job = c.start("1")
    r = c.step(job, batch=None)
    assert r["state"] == "done"
    titles = {row[0] for row in idx.db.execute("select title from pages")}
    assert titles == {"Page 1", "Page 2"}   # subtree survived the flake
