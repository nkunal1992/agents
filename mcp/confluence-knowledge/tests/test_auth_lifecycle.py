"""#177: consecutive 401s pause the job for a token rotation; on a fresh
credential the crawl resumes from where it stopped."""

import threading
import time

import httpx
import numpy as np
import pytest

from confluence_knowledge import server
from confluence_knowledge.client import RULES_DC, AccessError, ConfluenceClient
from confluence_knowledge.crawl import Crawler
from confluence_knowledge.index import Index
from confluence_knowledge.jobs import Progress, crawl_control


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


class _Embed:
    def embed(self, texts, query: bool = False):
        return np.ones((len(texts), 4), dtype=np.float32)


def _flipping_auth():
    """Token starts dead; factory re-reads `state` each build so the
    'rotated' flag simulates setup writing a new credential."""
    pages = {str(i): _page(str(i), ["1"]) for i in range(1, 6)}
    pages["1"]["ancestors"] = []
    children = {"1": [{"id": str(i)} for i in range(2, 6)]}
    state = {"rotated": False}

    def h(req):
        path = req.url.path
        if not state["rotated"]:
            return httpx.Response(401)
        if "user/current" in path:
            return httpx.Response(200, json={"username": "u"})
        if "/child/page" in path:
            pid = path.split("/content/")[1].split("/")[0]
            return httpx.Response(200, json={
                "results": children.get(pid, []),
                "size": len(children.get(pid, [])), "limit": 200})
        pid = path.split("/content/")[1].rstrip("/")
        return httpx.Response(200, json=pages[pid])

    return _client(h), lambda: _client(h), state


def test_auth_pause_then_resume(tmp_path, monkeypatch):
    from confluence_knowledge import jobs
    monkeypatch.setattr(jobs, "_AUTH_PROBE_S", 0.05)
    client, factory, state = _flipping_auth()
    idx = Index(tmp_path / "s.db")
    prog = Progress()
    c = Crawler(client, idx, "datacenter", client_factory=factory)
    job = c.start("1")
    idx.acquire_lease(job, server._owner.value, server._owner.pid)
    c.control = crawl_control(idx, job, prog, cancel_flag=None,
                              owner=server._owner, hb_s=60)
    c._embedder = _Embed()
    threading.Timer(0.6, lambda: state.update(rotated=True)).start()
    t0 = time.monotonic()
    r = c.step(job, batch=None)
    assert r["state"] == "done"                # paused, rotated, resumed
    assert time.monotonic() - t0 < 30
    assert {row[0] for row in idx.db.execute(
        "select title from pages")} == {f"Page {i}" for i in range(1, 6)}


def test_auth_pause_times_out(tmp_path, monkeypatch):
    from confluence_knowledge import jobs
    monkeypatch.setattr(jobs, "_AUTH_PROBE_S", 0.02)
    monkeypatch.setattr(jobs, "_AUTH_PAUSE_MAX_S", 0.2)
    client, factory, _ = _flipping_auth()
    idx = Index(tmp_path / "s.db")
    prog = Progress()
    c = Crawler(client, idx, "datacenter", client_factory=factory)
    job = c.start("1")
    idx.acquire_lease(job, server._owner.value, server._owner.pid)
    c.control = crawl_control(idx, job, prog, cancel_flag=None,
                              owner=server._owner, hb_s=60)
    c._embedder = _Embed()
    # pause ceiling → the step surfaces the auth error rather than
    # hanging forever; the worker loop would persist it as job "error"
    with pytest.raises(AccessError) as exc:
        c.step(job, batch=None)
    assert "auth pause timed out" in str(exc.value)


def test_auth_pause_notifies(tmp_path, monkeypatch):
    from confluence_knowledge import jobs
    monkeypatch.setattr(jobs, "_AUTH_PROBE_S", 0.02)
    monkeypatch.setattr(jobs, "_AUTH_PAUSE_MAX_S", 0.2)
    got = []
    client, factory, _ = _flipping_auth()
    idx = Index(tmp_path / "s.db")
    prog = Progress()
    c = Crawler(client, idx, "datacenter", client_factory=factory)
    job = c.start("1")
    idx.acquire_lease(job, server._owner.value, server._owner.pid)
    c.control = crawl_control(idx, job, prog, cancel_flag=None,
                              owner=server._owner, hb_s=60,
                              notify=lambda t, b: got.append((t, b)))
    c._embedder = _Embed()
    with pytest.raises(AccessError):
        c.step(job, batch=None)
    assert got and "token" in got[0][1]
