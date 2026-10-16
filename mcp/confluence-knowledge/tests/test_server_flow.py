"""End-to-end tool sequence the agent runs: connect -> start_crawl -> crawl_step.

Regression for #68: Index.__init__ left an uncommitted write transaction and
the server opened a fresh connection per tool call, so start_crawl
self-deadlocked with "database is locked".
"""

import sqlite3

import pytest

from confluence_knowledge import server
from confluence_knowledge.settings import SiteProfile


class _Resp:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d


class _FakeClient:
    def get(self, path, params=None):
        if path.endswith("user/current"):
            return _Resp({"username": "tester"})
        if "/child/page" in path:
            return _Resp({"results": [], "size": 0, "limit": 200})
        return _Resp({"id": "1", "title": "Root",
                      "body": {"storage": {"value": "<h1>Hi</h1><p>text</p>"}},
                      "version": {"number": 1},
                      "_links": {"webui": "/x", "base": "https://c"}})


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path))
    profile = SiteProfile("datacenter", "https://c", None, "ref")
    monkeypatch.setattr(SiteProfile, "load", staticmethod(lambda home: profile))
    monkeypatch.setattr(server, "_client_for", lambda p: _FakeClient())
    monkeypatch.setattr(server, "_embedder", lambda: None)
    real_connect = sqlite3.connect
    # fail fast instead of waiting out the 30s busy timeout
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **kw: real_connect(*a, **{**kw, "timeout": 1}))
    monkeypatch.setattr(server, "_scopes", {})
    monkeypatch.setattr(server, "_indexes", {})


def test_connect_then_crawl_does_not_lock(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    r = server.crawl_step(c["scope_handle"], job["job_id"])
    assert r["state"] == "done"
    assert r["pages_seen"] == 1


def test_reconnect_after_crawl_sees_cache(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    server.crawl_step(c["scope_handle"], job["job_id"])
    again = server.connect("1")
    assert again["cached"] is True
    assert again["cached_pages"] == 1
