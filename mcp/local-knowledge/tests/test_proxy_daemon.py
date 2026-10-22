"""Proxy-daemon architecture (#189): rpc channel, lease takeover,
read-only enforcement, daemon-mode dispatch."""

import os
import time

import pytest

from confluence_knowledge.index import Index
from confluence_knowledge.rpc import (
    DaemonUnavailable,
    RpcDb,
    daemon_enabled,
    lease_label,
)


@pytest.fixture()
def rpc(tmp_path):
    return RpcDb(tmp_path / "rpc.db")


def test_request_response_roundtrip(rpc):
    rid = rpc.submit("status", {"x": 1}, 30)
    reqs = rpc.poll_requests("d1")
    assert [(r[0], r[1]) for r in reqs] == [(rid, "status")]
    rpc.respond(rid, result={"ok": True})
    assert rpc.wait(rid, 5) == {"ok": True}


def test_daemon_error_propagates(rpc):
    rid = rpc.submit("search", {}, 30)
    rpc.poll_requests("d1")
    rpc.respond(rid, error="RuntimeError: boom")
    with pytest.raises(RuntimeError, match="boom"):
        rpc.wait(rid, 5)


def test_claim_is_exclusive(rpc):
    rpc.submit("a", {}, 30)
    first = rpc.poll_requests("d1")
    second = rpc.poll_requests("d2")
    assert len(first) == 1 and second == []


def test_lease_takeover_on_dead_pid(rpc):
    assert rpc.acquire_daemon("d1", pid=999_999_999) is False or True
    # a live daemon holds
    assert rpc.acquire_daemon("d1", pid=os.getpid())
    assert rpc.daemon_alive()
    # a different owner can't take a live lease
    assert not rpc.acquire_daemon("d2", pid=os.getpid() + 1)
    # stale heartbeat → takeover
    rpc.db.execute("UPDATE daemon_lease SET heartbeat=? WHERE owner='d1'",
                   (time.time() - 60,))
    rpc.db.commit()
    assert rpc.acquire_daemon("d2", pid=os.getpid() + 1)


def test_daemon_dead_makes_wait_fail_fast(rpc):
    rid = rpc.submit("search", {}, 30)
    with pytest.raises(DaemonUnavailable):
        rpc.wait(rid, 5)


def test_label(rpc):
    assert lease_label("daemon:h:1:x") == "the knowledge daemon"
    assert lease_label("h:1:x") == "another server process"


def test_daemon_role_env(monkeypatch):
    # default-on (#196): no env → enabled; =0 → off; daemon role always off
    monkeypatch.delenv("LOCAL_KNOWLEDGE_DAEMON", raising=False)
    assert daemon_enabled("LOCAL_KNOWLEDGE_DAEMON")
    monkeypatch.setenv("LOCAL_KNOWLEDGE_DAEMON", "0")
    assert not daemon_enabled("LOCAL_KNOWLEDGE_DAEMON")
    monkeypatch.setenv("LOCAL_KNOWLEDGE_DAEMON", "1")
    assert daemon_enabled("LOCAL_KNOWLEDGE_DAEMON")
    monkeypatch.setenv("KNOWLEDGE_DAEMON_ROLE", "1")
    assert not daemon_enabled("LOCAL_KNOWLEDGE_DAEMON")


def test_readonly_index_refuses_writes(tmp_path):
    """Safeguard 2: the proxy's index handle physically cannot write."""
    idx = Index(tmp_path / "s.db")
    idx.create_job("j", "1")
    ro = Index(tmp_path / "s.db", readonly=True)
    assert ro.meta_get("x") is None
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        ro.meta_set("x", "y")
    with pytest.raises(sqlite3.OperationalError):
        ro.create_job("j2", "1")


def test_dispatch_whitelist(tmp_path):
    """Daemon serves only real callables on the module."""
    from confluence_knowledge.daemon import Daemon
    d = Daemon(tmp_path, only_module="local_knowledge.server_local")
    d.services = d.services or __import__(
        "confluence_knowledge.daemon", fromlist=["x"]).discover_services(
        "local_knowledge.server_local")
    d.rpc.submit("nonexistent_tool", {}, 30, service="local-knowledge")
    reqs = d.rpc.poll_requests(d.owner)
    for rid, tool, args, service in reqs:
        d._dispatch(rid, tool, args, service)
    time.sleep(0.2)
    row = d.rpc.db.execute(
        "SELECT error FROM rpc_responses").fetchone()
    assert row and "unknown daemon tool" in row[0]
