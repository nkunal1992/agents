"""wait_for_crawl long-poll, last_crawl in status/connect, ETA fields,
and OS-notification dispatch/failure-safety."""

import threading
import time

import pytest
from test_server_flow import _FakeClient, wait_done

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
    monkeypatch.setattr(server, "_notified", set())
    monkeypatch.setattr(server, "notify", lambda *a, **k: True)


def test_wait_returns_on_terminal_state(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    wait_done(c["scope_handle"], job["job_id"])
    t0 = time.monotonic()
    r = server.wait_for_crawl(c["scope_handle"], job["job_id"], wait_s=50)
    assert time.monotonic() - t0 < 5     # terminal → returns immediately
    assert r["state"] == "done"
    assert r["changed"] is False


def test_wait_returns_early_on_change(wired, monkeypatch):
    """A phase flip mid-wait must break the loop immediately — this is the
    'user stays aware' path."""
    monkeypatch.setattr(server, "_ensure_worker", lambda s, j: False)
    c = server.connect("1")
    s = server._scopes[c["scope_handle"]]
    idx = server._index_for(s["key"])
    idx.create_job("j2", "1")
    idx.save_job("j2", "running", ["page:1"], [])
    prog = server._progress.setdefault("j2", server.Progress())
    def flip():
        time.sleep(0.4)
        prog.page_done(5)
    threading.Thread(target=flip, daemon=True).start()
    t0 = time.monotonic()
    r = server.wait_for_crawl(c["scope_handle"], "j2", wait_s=50)
    el = time.monotonic() - t0
    assert r["changed"] is True
    assert el < 10, f"wait_for_crawl didn't notice the change ({el:.1f}s)"
    assert r["pages_done"] == 5


def test_wait_times_out_bounded(wired, monkeypatch):
    monkeypatch.setattr(server, "_ensure_worker", lambda s, j: False)
    c = server.connect("1")
    s = server._scopes[c["scope_handle"]]
    idx = server._index_for(s["key"])
    idx.create_job("j3", "1")
    idx.save_job("j3", "running", ["page:1"], [])
    t0 = time.monotonic()
    r = server.wait_for_crawl(c["scope_handle"], "j3", wait_s=2)
    el = time.monotonic() - t0
    assert 2 <= el < 8 and r["changed"] is False and r["state"] == "running"


def test_wait_caps_wait_s(wired, monkeypatch):
    """wait_s is capped at 120 — verified with a fake clock so the test
    doesn't actually wait two minutes."""
    monkeypatch.setattr(server, "_ensure_worker", lambda s, j: False)
    c = server.connect("1")
    s = server._scopes[c["scope_handle"]]
    idx = server._index_for(s["key"])
    idx.create_job("j4", "1")
    idx.save_job("j4", "running", ["page:1"], [])
    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    r = server.wait_for_crawl(c["scope_handle"], "j4", wait_s=10**9)
    assert clock[0] <= 120            # capped — never waited a billion s
    assert r["changed"] is False


def test_progress_fields_rich(wired):
    """crawl_step progress now carries phase/rate/eta/elapsed/worker."""
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    r = wait_done(c["scope_handle"], job["job_id"])
    for k in ("phase", "rate_ppm", "eta_s", "elapsed_s",
              "pages_done", "remaining"):
        assert k in r, f"progress missing {k}"


def test_last_crawl_in_connect(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    wait_done(c["scope_handle"], job["job_id"])
    c2 = server.connect("1")
    assert c2["last_crawl"]["job_id"] == job["job_id"]
    assert c2["last_crawl"]["state"] == "done"
    assert "errors_n" in c2["last_crawl"]


def test_last_crawl_in_status(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    wait_done(c["scope_handle"], job["job_id"])
    st = server.status()
    assert st["last_crawl"]["state"] == "done"


def test_notify_fired_on_done(wired, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "notify",
                        lambda *a, **k: calls.append((a, k)) or True)
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    wait_done(c["scope_handle"], job["job_id"])
    # worker's finally fires the notify — give it a beat
    end = time.monotonic() + 5
    while not calls and time.monotonic() < end:
        time.sleep(0.05)
    assert calls, "no desktop notification fired on completion"
    assert "Crawl finished" in calls[0][0][0]


def test_notify_once_per_job(wired, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "notify",
                        lambda *a, **k: calls.append((a, k)) or True)
    c = server.connect("1")
    s = server._scopes[c["scope_handle"]]
    idx = server._index_for(s["key"])
    idx.create_job("j5", "1")
    idx.save_job("j5", "done", [], [])
    server._notify_terminal(idx, "j5")
    server._notify_terminal(idx, "j5")   # dedupe
    assert len(calls) == 1


def test_notify_running_job_notified_nothing(wired, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "notify",
                        lambda *a, **k: calls.append((a, k)) or True)
    c = server.connect("1")
    s = server._scopes[c["scope_handle"]]
    idx = server._index_for(s["key"])
    idx.create_job("j6", "1")
    idx.save_job("j6", "running", [], [])
    server._notify_terminal(idx, "j6")
    assert calls == []


def test_notify_helper_never_raises(monkeypatch):
    """notify() returns False (not an exception) when disabled or when the
    platform helper is missing/failing."""
    from confluence_knowledge import notify as n
    monkeypatch.setenv("KNOWLEDGE_NOTIFY", "0")
    assert n.notify("t", "b") is False
    monkeypatch.setenv("KNOWLEDGE_NOTIFY", "1")
    monkeypatch.setattr(n.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert n.notify("t", "b") is False
    # a helper that exits non-zero also yields False
    monkeypatch.setattr(n, "_sh", lambda cmd: False)
    assert n.notify("t", "b") is False
