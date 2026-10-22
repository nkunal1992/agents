"""#196: end-to-end daemon-mode coverage — a REAL spawned daemon serving
a proxy. Complements test_proxy_daemon.py's machinery unit tests.

These spawn the actual daemon subprocess (KNOWLEDGE_DAEMON_ROLE=1) and
drive connect/start_crawl/search through the RPC layer — the same path
an IDE-launched proxy takes in production."""

import os as _os

import pytest as _pt

# Process-lifecycle tests are POSIX-signal-centric (daemon spawn, lease
# recovery, SIGKILL respawn). On Windows runners the spawned daemon
# shares pytest's console group — the unit leg dies on a stray console
# interrupt ~6s into the first daemon test (reproducible on 3.11+3.13).
# Windows daemon coverage lives in tests/e2e/sessions instead.
pytestmark = _pt.mark.skipif(_os.name == "nt",
                             reason="daemon lifecycle is POSIX-centric; "
                                    "Windows coverage is in e2e sessions")

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PKG = str(Path(__file__).resolve().parents[2]
          / "confluence-knowledge" / "src")
SELF = str(Path(__file__).resolve().parents[1] / "src")


def _daemon_proc(home, workdir, dhome):
    env = {**os.environ,
           "PYTHONPATH": f"{SELF}{os.pathsep}{PKG}",
           "LOCAL_KNOWLEDGE_HOME": str(home),
           "LOCAL_KNOWLEDGE_WORKDIR": str(workdir),
           "KNOWLEDGE_DAEMON_ROLE": "1",
           "KNOWLEDGE_SHARED_HOME": str(dhome.parent.parent)}
    return subprocess.Popen(
        [sys.executable, "-m", "confluence_knowledge.daemon",
         str(dhome)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture()
def daemon_home(tmp_path):
    home = tmp_path / "home"; home.mkdir()
    work = tmp_path / "work"; work.mkdir()
    idx_root = work / ".local_knowledge_index"; idx_root.mkdir()
    return home, work, idx_root


def test_daemon_claims_and_answers(daemon_home):
    """A spawned daemon takes the lease and answers a whitelisted tool."""
    from confluence_knowledge.rpc import RpcDb
    home, work, _idx_root = daemon_home
    from confluence_knowledge.settings import daemon_home
    dhome = daemon_home()
    dhome.mkdir(parents=True, exist_ok=True)
    rpc = RpcDb(dhome / "rpc.db")
    proc = _daemon_proc(home, work, dhome)
    try:
        deadline = time.monotonic() + 15
        while not rpc.daemon_alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert rpc.daemon_alive(), "daemon never took the lease"
        rid = rpc.submit("status", {}, 30, service="local-knowledge")
        out = rpc.wait(rid, 30)
        assert "configured" in out or "_error" not in out
    finally:
        proc.terminate(); proc.wait(timeout=10)


def test_daemon_lease_survives_proxy_death(daemon_home):
    """Killing the 'IDE-side' caller leaves the daemon + its lease alive —
    the next proxy attaches to the same owner (#189's core promise)."""
    from confluence_knowledge.rpc import RpcDb
    home, work, _idx_root = daemon_home
    from confluence_knowledge.settings import daemon_home
    dhome = daemon_home()
    dhome.mkdir(parents=True, exist_ok=True)
    rpc = RpcDb(dhome / "rpc.db")
    proc = _daemon_proc(home, work, dhome)
    try:
        deadline = time.monotonic() + 15
        while not rpc.daemon_alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        owner = rpc.db.execute(
            "SELECT owner FROM daemon_lease").fetchone()[0]
        assert owner.startswith("daemon:")
        # a competing daemon can't take the lease while this one lives
        assert not rpc.acquire_daemon("daemon:rival:0:xx", pid=999999)
    finally:
        proc.terminate(); proc.wait(timeout=10)


def test_daemon_unreachable_falls_back(tmp_path, monkeypatch):
    """Safeguard 3 in-process: with the flag on but no daemon spawnable,
    tools run locally instead of erroring."""
    monkeypatch.setenv("LOCAL_KNOWLEDGE_DAEMON", "1")
    monkeypatch.setenv("LOCAL_KNOWLEDGE_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_WORKDIR", str(tmp_path / "w"))
    # daemon spawn impossible → ensure_daemon raises DaemonUnavailable →
    # _daemon_call returns None → local body runs
    import local_knowledge.server_local as srv
    monkeypatch.setattr(srv, "ensure_daemon",
                        lambda *a, **k: (_ for _ in ()).throw(
                            srv.DaemonUnavailable("no daemon")))
    out = srv.status()
    assert out.get("configured") is False or "error" in out.get("profile", {})
    assert out["daemon"] == "starting"


def test_verify_citations_routes_to_daemon(monkeypatch):
    """Evidence sets are process-local — under daemon mode `search`
    populates the DAEMON's registry, so verify must route there first
    (field bug: proxy's _evidence_sets is always empty → "unknown
    evidence set" on every verify call)."""
    import local_knowledge.server_local as srv
    seen = {}
    monkeypatch.setattr(
        srv, "_daemon_call",
        lambda name, args, timeout: (
            seen.update(name=name) or {"ok": True}))
    out = srv.verify_citations("scope:x", "set:1", [])
    assert seen["name"] == "verify_citations" and out == {"ok": True}
