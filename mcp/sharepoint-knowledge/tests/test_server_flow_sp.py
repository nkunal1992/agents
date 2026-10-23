"""Full server flow against the in-process fake Graph — connect → crawl →
search → evidence, with a FakeEmbedder/Reranker monkeypatched in."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _fake_graph import BLOBS, SITE_ID, FakeGraph, site_routes


class FakeEmbedder:
    def embed(self, texts, query: bool = False):
        return np.random.rand(len(texts), 8).astype(np.float32)


class FakeReranker:
    def __init__(self, _home=None, _cli=None):
        pass

    def score(self, query, docs):
        return np.arange(len(docs), dtype=np.float32)[::-1]


class FakeSparse:
    def embed(self, texts, query: bool = False):
        return [{} for _ in texts]


@pytest.fixture()
def sp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SP_KNOWLEDGE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SP_KNOWLEDGE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("SP_KNOWLEDGE_TOKEN", "fake-e2e-token")
    from sharepoint_knowledge import server_sp, settings_sp

    # a profile exists so SPProfile.load() works
    settings_sp.home().mkdir(parents=True, exist_ok=True)
    settings_sp.SPProfile("t-tenant", "t-client",
                          "https://t.sharepoint.com/sites/x",
                          "ref").save()

    server_sp._EMBEDDER = FakeEmbedder()
    server_sp._RERANKER = FakeReranker()
    server_sp._SPARSE = FakeSparse()
    server_sp._models_ready.set()

    # the real client goes through httpx — replace it with the fake
    fake = FakeGraph(site_routes())
    server_sp._clients["https://t.sharepoint.com/sites/x"] = (
        "fake-e2e-token", fake)
    monkeypatch.setattr(server_sp, "download_file",
                        lambda url: BLOBS[url])
    return server_sp


def test_full_flow(sp_env):
    c = sp_env.connect("site:" + SITE_ID)
    job = sp_env.start_crawl(c["scope_handle"])
    r = sp_env.crawl_step(c["scope_handle"], job["job_id"])
    while r["state"] == "running":
        r = sp_env.crawl_step(c["scope_handle"], job["job_id"])
    assert r["pages_seen"] == 3
    assert any("secret" in w for w in r["warnings"])

    res = sp_env.search(c["scope_handle"], "when are deploys allowed?")
    assert res["coverage"] == "complete"
    titles = {e["page_title"] for e in res["evidence"]}
    assert titles
    # every evidence row carries a citation URL + chunk id
    for e in res["evidence"]:
        assert e["page_url"].startswith("https://")
        ev = sp_env.get_evidence(c["scope_handle"], [e["chunk_id"]])
        assert ev["chunks"]


def test_status_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv("SP_KNOWLEDGE_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("SP_KNOWLEDGE_WORKDIR", str(tmp_path / "w"))
    from sharepoint_knowledge import server_sp
    st = server_sp.status()
    assert st["configured"] is False and "error" in st["profile"]


def test_scope_rebind_from_meta(sp_env):
    """#229: after a restart (_scopes empty) a cached scope re-binds from
    the persisted `root`/`root_entry` meta — no Graph calls, no KeyError
    on a missing 'scope_handle'."""
    c = sp_env.connect("site:" + SITE_ID)
    sp_env._scopes.clear()
    s = sp_env._scope(c["scope_handle"])
    assert s["key"] == c["scope_handle"].split(":")[1]
    assert s["root_entry"]
    from confluence_knowledge.settings import SetupError
    key = c["scope_handle"].split(":")[1]
    for bad in (f"scope:{key}:bogus", "scope:deadbeef:site:x", "scope:x"):
        try:
            sp_env._scope(bad)
            raise SystemExit(f"{bad} accepted")
        except SetupError:
            pass


def test_verify_survives_restart(sp_env):
    """#231: same restart contract as confluence — _evidence_sets is
    process memory; the persisted shadow (chunk_ids + epoch in index
    meta) rebuilds the set after a daemon restart / new chat."""
    c = sp_env.connect("site:" + SITE_ID)
    job = sp_env.start_crawl(c["scope_handle"])
    r = sp_env.crawl_step(c["scope_handle"], job["job_id"])
    while r["state"] == "running":
        r = sp_env.crawl_step(c["scope_handle"], job["job_id"])
    res = sp_env.search(c["scope_handle"], "when are deploys allowed?")
    set_id = res["evidence_set"]
    q = res["evidence"][0]
    mapping = [{"claim": "x", "chunk_id": q["chunk_id"],
                "quote": q["text"][:12]}]
    sp_env._evidence_sets.clear()          # daemon restarted
    v = sp_env.verify_citations(c["scope_handle"], set_id, mapping)
    assert v["ok"], v
