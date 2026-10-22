"""Real-daemon round-trips for confluence-knowledge — spawned daemon +
fake-DC HTTP server + fake embeddings, driven through the proxy path.
Catches the process-local split-brain class (verify_citations, progress)
that unit tests structurally cannot see."""

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

TESTS = Path(__file__).resolve().parent
PKG = str(TESTS.parent / "src")
SCRIPTS = str(TESTS.parent / "scripts")
sys.path.insert(0, SCRIPTS)

from _e2e_common import TOKEN, serve


def _daemon(home, work, dhome, base):
    env = {**os.environ,
           "PYTHONPATH": PKG,
           "CONF_KNOWLEDGE_HOME": str(home),
           "CONF_KNOWLEDGE_WORKDIR": str(work),
           "CONF_KNOWLEDGE_TOKEN": TOKEN,
           "KNOWLEDGE_DAEMON_ROLE": "1",
           "KNOWLEDGE_FAKE_EMBEDDINGS": "1",
           "KNOWLEDGE_SHARED_HOME": str(dhome.parent.parent),
           "CONF_KNOWLEDGE_BASE_URL": base}
    return subprocess.Popen(
        [sys.executable, "-m", "confluence_knowledge.daemon",
         str(dhome)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    work = tmp_path / "work"; work.mkdir()
    idx_root = work / ".conf_knowledge_index"; idx_root.mkdir()

    srv, base, _hits = serve()
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(home))
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(work))
    monkeypatch.setenv("CONF_KNOWLEDGE_DAEMON", "1")
    monkeypatch.setenv("KNOWLEDGE_SHARED_HOME",
                       str(tmp_path / "shared"))
    monkeypatch.setenv("KNOWLEDGE_FAKE_EMBEDDINGS", "1")
    monkeypatch.setenv("CONF_KNOWLEDGE_TOKEN", TOKEN)

    # the daemon reads the profile file — write it the way setup would
    from confluence_knowledge.settings import SiteProfile
    SiteProfile("datacenter", base, None, "e2e").save(home)

    import importlib

    import confluence_knowledge.server as csrv
    from confluence_knowledge.rpc import RpcDb
    importlib.reload(csrv)
    from confluence_knowledge.settings import daemon_home
    dhome = daemon_home()
    dhome.mkdir(parents=True, exist_ok=True)
    rpc = RpcDb(dhome / "rpc.db")
    proc = _daemon(home, work, dhome, base)
    deadline = time.monotonic() + 20
    while not rpc.daemon_alive() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert rpc.daemon_alive(), "daemon never claimed the lease"
    yield csrv, proc
    proc.terminate(); proc.wait(timeout=10)
    srv.shutdown()


def test_full_rag_tail_through_daemon(rig):
    """connect → crawl → search → verify → feedback across the boundary."""
    srv, _ = rig
    c = srv.connect("100")               # fake DC's root page
    assert c.get("scope_handle"), c
    j = srv.start_crawl(c["scope_handle"])
    final = {"state": "starting"}
    deadline = time.monotonic() + 120
    while final["state"] == "running" or final["state"] == "starting" \
            or final.get("phase", "").startswith(("starting", "fetch")):
        if time.monotonic() > deadline:
            break
        final = srv.wait_for_crawl(c["scope_handle"], j["job_id"],
                                   wait_s=25)
        if final["state"] == "running" and \
                final.get("phase") == "embedding" and \
                (final.get("remaining") or 0) == 0:
            pass  # keep polling — wait_for_crawl returns on any change
    assert final["state"] == "done", final.get("phase")
    res = srv.search(c["scope_handle"], "release sign-off")
    assert res["evidence"], "nothing retrieved through the daemon"
    ok = srv.verify_citations(c["scope_handle"], res["evidence_set"], [
        {"claim": "x", "chunk_id": res["evidence"][0]["chunk_id"],
         "quote": res["evidence"][0]["text"][:40]}])
    assert ok["ok"] is True
    fb = srv.submit_feedback(c["scope_handle"], "q", "correction")
    assert isinstance(fb, dict)
