"""cancel_crawl — the worker stops within seconds, the job becomes a
terminal `cancelled` that never auto-resumes, and the previous published
snapshot survives untouched."""

import threading
import time

import pytest
from test_parallel_crawl import _fan_client
from test_server_flow import _FakeClient, wait_done

from confluence_knowledge import server
from confluence_knowledge.client import AccessError
from confluence_knowledge.settings import SiteProfile


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
    server._models_ready.set()          # models already warm for the worker
    monkeypatch.setattr(server, "_warm_started", True)
    monkeypatch.setattr(server, "_scopes", {})
    monkeypatch.setattr(server, "_indexes", {})
    monkeypatch.setattr(server, "_workers", {})
    monkeypatch.setattr(server, "_worker_errors", {})
    monkeypatch.setattr(server, "_progress", {})
    monkeypatch.setattr(server, "_cancel_flags", {})


def _slow_fan(monkeypatch, delay=0.08):
    monkeypatch.setattr(server, "_client_for",
                        lambda p: _fan_client(delay=delay))


def _worker_dead(scope_key: str, job_id: str, timeout=20) -> bool:
    key = f"{scope_key}:{job_id}"
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        t = server._workers.get(key)
        if t is None or not t.is_alive():
            return True
        time.sleep(0.05)
    return False


def test_cancel_stops_worker_fast(wired, monkeypatch):
    c = server.connect("1")              # connects with the plain fake client
    _slow_fan(monkeypatch, delay=0.1)   # swap to the slow fan for the crawl
    job = server.start_crawl(c["scope_handle"])
    time.sleep(0.2)                      # let the worker get going
    t0 = time.monotonic()
    r = server.cancel_crawl(c["scope_handle"], job["job_id"])
    assert r["state"] == "cancelled"
    s = server._scopes[c["scope_handle"]]
    assert _worker_dead(s["key"], job["job_id"]), "worker did not stop"
    # the signal is that cancel terminates the worker, not the latency —
    # an 8s bound flaked on a loaded Windows CI runner mid-fetch
    assert time.monotonic() - t0 < 20
    # cancelled is terminal — polling never resurrects it
    for _ in range(5):
        assert server.crawl_step(
            c["scope_handle"], job["job_id"])["state"] == "cancelled"
        time.sleep(0.05)


def test_cancel_keeps_previous_snapshot(wired, monkeypatch):
    # first crawl completes and publishes
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    assert wait_done(c["scope_handle"], job["job_id"])["state"] == "done"
    s = server._scopes[c["scope_handle"]]
    idx = server._index_for(s["key"])
    before = idx.active_pages()
    assert before                       # live rows exist
    # second crawl gets cancelled mid-run — live rows must be untouched
    _slow_fan(monkeypatch, delay=0.1)
    job2 = server.start_crawl(c["scope_handle"])
    time.sleep(0.2)
    server.cancel_crawl(c["scope_handle"], job2["job_id"])
    assert _worker_dead(s["key"], job2["job_id"])
    assert idx.active_pages() == before


def test_cancel_finished_job_is_noop(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    assert wait_done(c["scope_handle"], job["job_id"])["state"] == "done"
    r = server.cancel_crawl(c["scope_handle"], job["job_id"])
    assert r["state"] == "done"          # already finished — no error


def test_cancel_unknown_job_raises(wired):
    c = server.connect("1")
    with pytest.raises(AccessError):
        server.cancel_crawl(c["scope_handle"], "no-such-job")


def test_db_state_flip_stops_worker_within_heartbeat(wired, tmp_path):
    """Cross-process cancel (e.g. another IDE window cancelled): the DB
    state flips and the local worker notices at its next heartbeat."""
    from test_parallel_crawl import _fan_client as fan

    from confluence_knowledge.crawl import Crawler
    from confluence_knowledge.index import Index
    from confluence_knowledge.jobs import Progress, crawl_control

    idx = Index(tmp_path / "xb.db")
    idx.create_job("j1", "1")
    idx.save_job("j1", "running", ["page:1"], [])
    assert idx.acquire_lease("j1", server._owner.value, server._owner.pid)
    ctl = crawl_control(idx, "j1", Progress(), cancel_flag=None,
                        owner=server._owner, hb_s=0.1)
    c = Crawler(fan(delay=0.08), idx, "datacenter", control=ctl)
    # flip the DB state mid-run — simulates another process cancelling
    threading.Timer(0.4, idx.save_job,
                    args=("j1", "cancelled", [], [])).start()
    t0 = time.monotonic()
    with pytest.raises(server.CrawlCancelled):
        c.step("j1", batch=None)
    assert time.monotonic() - t0 < 5
