"""Unit contracts for the #206 request-bound hardening — the waits that
let a request outlive the client's timeout (the field's -32001)."""
from __future__ import annotations

import threading
import time

import pytest
from test_server_flow import _FakeClient

from confluence_knowledge import server
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
    server._models_ready.set()
    monkeypatch.setattr(server, "_warm_started", True)
    monkeypatch.setattr(server, "_scopes", {})
    monkeypatch.setattr(server, "_indexes", {})
    monkeypatch.setattr(server, "_workers", {})
    monkeypatch.setattr(server, "_worker_errors", {})
    monkeypatch.setattr(server, "_progress", {})
    monkeypatch.setattr(server, "_cancel_flags", {})


def test_wait_for_crawl_caps_at_45(wired, monkeypatch):
    """wait_s=120 must return inside ~46s even when nothing moves — a
    longer in-flight request outlives any IDE request timeout."""
    from confluence_knowledge import server
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    # hold the job mid-run so the wait can't early-return on 'done'
    monkeypatch.setattr(server, "_progress_for",
                        lambda idx, jid: {"state": "running",
                                          "phase": "fetching",
                                          "pages_done": 1,
                                          "total_pages": 5})
    t0 = time.monotonic()
    out = server.wait_for_crawl(c["scope_handle"], job["job_id"],
                                wait_s=120)
    elapsed = time.monotonic() - t0
    assert elapsed < 50, f"wait ran {elapsed:.1f}s — the 45s cap failed"
    assert out["changed"] is False


def test_wait_for_crawl_reports_sleep(wired, monkeypatch):
    """A >15s wall-clock jump between polls = the machine suspended — the
    tool returns a wake note instead of hanging to the deadline."""
    from confluence_knowledge import server
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    monkeypatch.setattr(server, "_progress_for",
                        lambda idx, jid: {"state": "running",
                                          "phase": "fetching",
                                          "pages_done": 1,
                                          "total_pages": 5})
    # deterministic suspend: a fake wall clock + a fake sleep that jumps
    # it +30 s on the first loop nap — the next `now` read sees the gap.
    # (heartbeat/acquire_lease read time.time() on their own schedule —
    # counting calls is order-fragile; advancing the clock itself isn't.)
    real_time = time.time
    fake_now = [real_time()]
    jumped = {"once": True}
    def fake_sleep(s):
        if jumped.pop("once", None):
            fake_now[0] += 30        # the laptop slept mid-wait
    monkeypatch.setattr(time, "time", lambda: fake_now[0])
    monkeypatch.setattr(time, "sleep", fake_sleep)
    out = server.wait_for_crawl(c["scope_handle"], job["job_id"],
                                wait_s=45)
    assert "slept" in out.get("note", "") or "note" in out


def test_rpc_wait_wall_clock_ceiling(tmp_path, monkeypatch):
    """time.monotonic can stall across suspend — the wall deadline still
    bounds the wait."""
    from confluence_knowledge.rpc import DaemonUnavailable, RpcDb
    rpc = RpcDb(tmp_path / "rpc.db")
    rid = rpc.submit("nope", {}, 5)
    monkeypatch.setattr(rpc, "daemon_alive", lambda: True)
    # freeze monotonic — the monotonic deadline never arrives
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    t0 = time.time()
    try:
        rpc.wait(rid, 1)
        raise AssertionError("wait must end")
    except DaemonUnavailable:
        pass
    assert time.time() - t0 < 12
