"""Real-daemon round-trips for sharepoint-knowledge — spawned daemon +
fake-Graph HTTP server + fake embeddings, through the proxy path."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
SP_ROOT = TESTS.parent
SELF = str(SP_ROOT / "src")
PKG = str(SP_ROOT.parent / "confluence-knowledge" / "src")
sys.path.insert(0, str(SP_ROOT / "scripts"))

from _fake_graph_server import TOKEN, serve


def _daemon(home, work, dhome, graph_base):
    env = {**os.environ,
           "PYTHONPATH": f"{SELF}{os.pathsep}{PKG}",
           "SP_KNOWLEDGE_HOME": str(home),
           "SP_KNOWLEDGE_WORKDIR": str(work),
           "SP_KNOWLEDGE_TOKEN": TOKEN,
           "SP_KNOWLEDGE_GRAPH_BASE": graph_base,
           "KNOWLEDGE_DAEMON_ROLE": "1",
           "KNOWLEDGE_SHARED_HOME": str(dhome.parent.parent),
           "KNOWLEDGE_FAKE_EMBEDDINGS": "1"}
    return subprocess.Popen(
        [sys.executable, "-m", "confluence_knowledge.daemon",
         str(dhome)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    work = tmp_path / "work"; work.mkdir()
    idx_root = work / ".sharepoint_knowledge_index"; idx_root.mkdir()

    srv, graph_base, _hits, _noauth = serve()
    monkeypatch.setenv("SP_KNOWLEDGE_HOME", str(home))
    monkeypatch.setenv("SP_KNOWLEDGE_WORKDIR", str(work))
    monkeypatch.setenv("SP_KNOWLEDGE_DAEMON", "1")
    monkeypatch.setenv("KNOWLEDGE_SHARED_HOME",
                       str(tmp_path / "shared"))
    monkeypatch.setenv("SP_KNOWLEDGE_GRAPH_BASE", graph_base)
    monkeypatch.setenv("SP_KNOWLEDGE_TOKEN", TOKEN)
    monkeypatch.setenv("KNOWLEDGE_FAKE_EMBEDDINGS", "1")

    from urllib.parse import urlparse

    from sharepoint_knowledge.settings_sp import SPProfile
    site_url = (f"http://127.0.0.1:{urlparse(graph_base).port}"
                "/sites/team")
    SPProfile("tenant", "client", site_url, "e2e").save()

    import importlib

    import sharepoint_knowledge.server_sp as sp
    from confluence_knowledge.rpc import RpcDb
    importlib.reload(sp)
    from confluence_knowledge.settings import daemon_home
    dhome = daemon_home()
    dhome.mkdir(parents=True, exist_ok=True)
    rpc = RpcDb(dhome / "rpc.db")
    proc = _daemon(home, work, dhome, graph_base)
    deadline = time.monotonic() + 20
    while not rpc.daemon_alive() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert rpc.daemon_alive(), "daemon never claimed the lease"
    yield sp, proc, site_url
    proc.terminate(); proc.wait(timeout=10)
    srv.shutdown()


def test_full_rag_tail_through_daemon(rig):
    srv, _, _site_url = rig
    c = srv.connect(_site_url)
    assert c.get("scope_handle"), c
    assert c.get("scope_handle"), c
    j = srv.start_crawl(c["scope_handle"])
    final = {"state": "starting"}
    deadline = time.monotonic() + 120
    while final["state"] in ("starting", "running") \
            and time.monotonic() < deadline:
        final = srv.wait_for_crawl(c["scope_handle"], j["job_id"],
                                   wait_s=25)
    assert final["state"] == "done", final.get("phase")
    res = srv.search(c["scope_handle"], "release sign-off")
    assert res["evidence"], "nothing retrieved through the daemon"
    ok = srv.verify_citations(c["scope_handle"], res["evidence_set"], [
        {"claim": "x", "chunk_id": res["evidence"][0]["chunk_id"],
         "quote": res["evidence"][0]["text"][:40]}])
    assert ok["ok"] is True
