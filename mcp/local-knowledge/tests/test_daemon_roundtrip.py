"""Real-daemon round-trips — a spawned daemon process + fake embeddings
serving the actual proxy path (server tools → _daemon_call → RpcDb →
daemon). This is the coverage that catches process-local split-brain:
the verify_citations bug passed every unit suite because no test had a
daemon holding the registry while a proxy called."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

MCP_ROOT = Path(__file__).resolve().parents[2]
SELF = str(Path(__file__).resolve().parents[1] / "src")
PKG = str(MCP_ROOT / "confluence-knowledge" / "src")


def _daemon(home, work, idx_root):
    env = {**os.environ,
           "PYTHONPATH": f"{SELF}{os.pathsep}{PKG}",
           "LOCAL_KNOWLEDGE_HOME": str(home),
           "LOCAL_KNOWLEDGE_WORKDIR": str(work),
           "KNOWLEDGE_DAEMON_ROLE": "1",
           "KNOWLEDGE_FAKE_EMBEDDINGS": "1"}
    return subprocess.Popen(
        [sys.executable, "-m", "confluence_knowledge.daemon",
         "local_knowledge.server_local", str(idx_root)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    """Spawned daemon + proxy-wired server module state."""
    home = tmp_path / "home"; home.mkdir()
    work = tmp_path / "work"; work.mkdir()
    tree = tmp_path / "tree"; tree.mkdir()
    for i in range(6):
        (tree / f"doc{i}.md").write_text(
            f"unique codeword alpha{i} " + "filler text " * 40)
    idx_root = work / ".local_knowledge_index"; idx_root.mkdir()

    monkeypatch.setenv("LOCAL_KNOWLEDGE_HOME", str(home))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_WORKDIR", str(work))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_DAEMON", "1")
    monkeypatch.setenv("KNOWLEDGE_FAKE_EMBEDDINGS", "1")

    from confluence_knowledge.rpc import RpcDb
    import local_knowledge.server_local as srv
    import importlib
    importlib.reload(srv)          # pick up fresh env/global state
    rpc = RpcDb(idx_root / "rpc.db")
    proc = _daemon(home, work, idx_root)
    deadline = time.monotonic() + 20
    while not rpc.daemon_alive() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert rpc.daemon_alive(), "daemon never claimed the lease"
    yield srv, tree, proc
    proc.terminate(); proc.wait(timeout=10)


def test_full_rag_tail_through_daemon(rig):
    """connect → crawl → search → verify → feedback — every boundary that
    holds process-local state must behave identically proxy-side."""
    srv, tree, _ = rig
    c = srv.connect(str(tree))
    assert c.get("scope_handle"), c
    j = srv.start_crawl(c["scope_handle"])
    final = srv.wait_for_crawl(c["scope_handle"], j["job_id"], wait_s=60)
    assert final["state"] == "done", final.get("phase")
    res = srv.search(c["scope_handle"], "codeword alpha3")
    assert res["evidence"], "hash-embedder retrieval returned nothing"
    ok = srv.verify_citations(c["scope_handle"], res["evidence_set"], [
        {"claim": "x", "chunk_id": res["evidence"][0]["chunk_id"],
         "quote": res["evidence"][0]["text"][:40]}])
    assert ok["ok"] is True
    fb = srv.submit_feedback(c["scope_handle"], "codeword?", "it is alpha")
    assert fb.get("ok") or fb.get("saved") is not False
    rep = srv.feedback_report(c["scope_handle"])
    assert isinstance(rep, dict)


def test_daemon_kill_mid_crawl_orphan_resumes(rig, monkeypatch):
    """SIGKILL the daemon mid-crawl → next proxy call respawns it → the
    orphaned running job is reclaimed and finishes (DAEKILL path)."""
    srv, tree, proc = rig
    c = srv.connect(str(tree))
    j = srv.start_crawl(c["scope_handle"], refresh=True)
    proc.kill(); proc.wait(timeout=10)
    # next tool call must respawn the daemon — no user action
    st = srv.crawl_step(c["scope_handle"], j["job_id"])
    assert st["state"] in ("running", "done"), st
    final = srv.wait_for_crawl(c["scope_handle"], j["job_id"], wait_s=90)
    assert final["state"] == "done"
