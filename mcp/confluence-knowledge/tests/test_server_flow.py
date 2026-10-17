"""End-to-end tool sequence the agent runs: connect -> start_crawl -> crawl_step.

Regression for #68: Index.__init__ left an uncommitted write transaction and
the server opened a fresh connection per tool call, so start_crawl
self-deadlocked with "database is locked".
"""

import sqlite3
import threading
import time

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
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path / "home"))
    profile = SiteProfile("datacenter", "https://c", None, "ref")
    monkeypatch.setattr(SiteProfile, "load", staticmethod(lambda home: profile))
    monkeypatch.setattr(server, "_client_for", lambda p: _FakeClient())
    monkeypatch.setattr(server, "_embedder", lambda: None)
    monkeypatch.setattr(server, "_reranker", lambda: None)
    monkeypatch.setattr(server, "_sparse", lambda: None)
    monkeypatch.setattr(server, "_models_ready", threading.Event())
    monkeypatch.setattr(server, "_warm_started", False)
    real_connect = sqlite3.connect
    # fail fast instead of waiting out the 30s busy timeout
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **kw: real_connect(*a, **{**kw, "timeout": 1}))
    monkeypatch.setattr(server, "_scopes", {})
    monkeypatch.setattr(server, "_indexes", {})
    monkeypatch.setattr(server, "_workers", {})
    monkeypatch.setattr(server, "_worker_errors", {})


def wait_done(scope, job_id, timeout=15):
    """The background worker does the crawling; crawl_step polls."""
    end = time.monotonic() + timeout
    while True:
        r = server.crawl_step(scope, job_id)
        if r["state"] != "running":
            return r
        assert time.monotonic() < end, "background crawl did not finish"
        time.sleep(0.02)


def test_connect_then_crawl_does_not_lock(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    r = wait_done(c["scope_handle"], job["job_id"])
    assert r["state"] == "done"
    assert r["pages_seen"] == 1


def test_background_crawl_worker_does_the_work(wired):
    """#81: start_crawl returns instantly and the crawl completes on its
    own — crawl_step is only an instant progress read, so a big subtree
    doesn't need one LLM turn per page."""
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    # the job may still be running — crawl_step must return instantly anyway
    t = time.monotonic()
    r = server.crawl_step(c["scope_handle"], job["job_id"])
    assert time.monotonic() - t < 2
    assert r["state"] in ("running", "done")
    r = wait_done(c["scope_handle"], job["job_id"])
    assert r["state"] == "done" and r["pages_seen"] == 1


class _FakeEmbedder:
    def embed(self, texts):
        import numpy as np
        return np.ones((len(texts), 4), dtype=np.float32)


class _FakeReranker:
    def score(self, query, docs):
        import numpy as np
        # descending 0.9, 0.8, … — above the 0.3 evidence floor
        return np.linspace(0.9, 0.5, len(docs), dtype=np.float32)


def test_full_agent_flow_with_models(wired, monkeypatch):
    """connect → crawl (embedding on) → search (rerank on) → evidence → citations."""
    emb, rr = _FakeEmbedder(), _FakeReranker()
    monkeypatch.setattr(server, "_embedder", lambda: emb)
    monkeypatch.setattr(server, "_reranker", lambda: rr)
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    assert wait_done(c["scope_handle"], job["job_id"])["state"] == "done"

    res = server.search(c["scope_handle"], "text")
    assert res["evidence"], res
    top = res["evidence"][0]
    assert top["page_title"] == "Root" and top["page_url"]

    full = server.get_evidence(c["scope_handle"], [top["chunk_id"]])
    assert "text" in full["chunks"][0]["text"]

    ok = server.verify_citations(c["scope_handle"], res["evidence_set"], [
        {"claim": "c", "chunk_id": top["chunk_id"], "quote": "text"}])
    assert ok["ok"] is True


def test_search_during_crawl_says_crawling(wired, monkeypatch):
    # keep the worker parked on the model-warmup gate so the job stays
    # "running" deterministically: a never-set event AND warmup disabled —
    # otherwise the warmup thread sets the event, the worker finishes the
    # 1-page crawl, and search reaches retrieve instead of the early return
    monkeypatch.setattr(server, "_models_ready", threading.Event())
    monkeypatch.setattr(server, "_start_warmup", lambda: None)
    c = server.connect("1")
    server.start_crawl(c["scope_handle"])
    res = server.search(c["scope_handle"], "text")
    assert res["coverage"] == "crawling" and "crawl_step" in res["note"]


def test_crawl_step_returns_while_models_load(wired, monkeypatch):
    """The first torch/model load can exceed the IDE timeout — the tool must
    answer 'loading' instead of blocking."""
    # models never finish loading — the warmup gate stays closed so the
    # worker parks and the tool call must still answer instantly
    monkeypatch.setattr(server, "_start_warmup", lambda: None)
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    r = server.crawl_step(c["scope_handle"], job["job_id"])
    assert r["note"] == server.LOADING_NOTE


def test_scope_survives_server_restart(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    server._scopes.clear()                    # IDE restarted the server
    server._workers.clear()                   # and the worker died with it
    assert wait_done(c["scope_handle"], job["job_id"])["state"] == "done"


def test_coverage_complete_despite_earlier_failed_job(wired, monkeypatch):
    emb, rr = _FakeEmbedder(), _FakeReranker()
    monkeypatch.setattr(server, "_embedder", lambda: emb)
    monkeypatch.setattr(server, "_reranker", lambda: rr)
    c = server.connect("1")
    server.start_crawl(c["scope_handle"])     # abandoned
    job = server.start_crawl(c["scope_handle"])
    wait_done(c["scope_handle"], job["job_id"])
    assert server.search(c["scope_handle"], 'say "text')["coverage"] == "complete"


def test_reconnect_after_crawl_sees_cache(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    wait_done(c["scope_handle"], job["job_id"])
    again = server.connect("1")
    assert again["cached"] is True
    assert again["cached_pages"] == 1
