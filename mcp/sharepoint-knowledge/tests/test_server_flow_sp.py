"""Full server flow against the in-process fake Graph — connect → crawl →
search → evidence, with a FakeEmbedder/Reranker monkeypatched in."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _fake_graph import BLOBS, SITE_ID, FakeGraph, site_routes

from confluence_knowledge.client import SecurityException


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
    yield server_sp
    # cached scopes/indexes are process state — without a reset the next
    # test's fresh workdir connects hit the previous test's Index objects
    server_sp._scopes.clear()
    server_sp._indexes.clear()
    server_sp._evidence_sets.clear()
    server_sp._models_ready.clear()


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


def test_status_lists_persisted_scope_after_restart(sp_env, monkeypatch):
    """#263: a new chat (empty _scopes) must see the persisted index —
    indexed_scopes carries the handle the disk re-bind path accepts."""
    _sp_crawl(sp_env, monkeypatch)
    sp_env._scopes.clear()                   # simulate new chat/respawn
    st = sp_env.status()
    scopes = st["indexed_scopes"]
    assert len(scopes) == 1
    assert scopes[0]["scope_handle"].startswith("scope:")
    assert scopes[0]["root"] == f"site:{SITE_ID}"
    assert scopes[0]["pages"] > 0
    assert scopes[0]["last_crawl"]["state"] == "done"


def test_search_without_handle_uses_single_index(sp_env, monkeypatch):
    """#263: no URL, one index on disk → search uses it without the agent
    needing the handle — and echoes it back for the evidence chain."""
    c = _sp_crawl(sp_env, monkeypatch)
    sp_env._scopes.clear()
    res = sp_env.search("", "deploys")
    assert res["evidence"] and res["coverage"] in ("complete", "partial")
    assert res["scope_handle"] == c["scope_handle"]


def test_search_without_handle_no_index(tmp_path, monkeypatch):
    """Nothing indexed → 'unconfigured' so the agent runs connect, not a
    fabricated search."""
    monkeypatch.setenv("SP_KNOWLEDGE_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("SP_KNOWLEDGE_WORKDIR", str(tmp_path / "w"))
    from sharepoint_knowledge import server_sp
    res = server_sp.search("", "anything")
    assert res["evidence"] == [] and res["coverage"] == "unconfigured"
    assert "connect" in res["note"]


def test_search_without_handle_ambiguous(sp_env, monkeypatch):
    """Two usable indexes → 'ambiguous' naming candidates — never a
    silent guess across subtrees."""
    _sp_crawl(sp_env, monkeypatch)
    # stamp a second usable scope index directly — enough for
    # stats().pages > 0 without a second fake-graph site
    from confluence_knowledge.index import Index
    from sharepoint_knowledge.settings_sp import index_root
    idx2 = Index(index_root() / "otherscope.db")
    idx2.meta_set("root", "site:other-guid")
    idx2.meta_set("root_entry", "site:other-guid")
    idx2.db.execute(
        "INSERT INTO pages (page_id, title, url, fetched_at, active) "
        "VALUES ('p9', 'Other', 'https://t/o', 1, 1)")
    idx2.db.commit()
    sp_env._scopes.clear()
    res = sp_env.search("", "deploys")
    assert res["coverage"] == "ambiguous"
    assert {c["root"] for c in res["candidates"]} == \
        {f"site:{SITE_ID}", "site:other-guid"}


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


def _routes_with_subtree() -> dict:
    """site_routes + a 'policies' folder holding two real files — the
    #240 alias-test subtree (item:{DRIVE_ID}:f1 → f4,f5 pages)."""
    from _fake_graph import DRIVE_ID as _D
    r = site_routes()
    r[f"drives/{_D}/items/f1/children"] = {"value": [
        {"id": "f4", "name": "policy.md"},
        {"id": "f5", "name": "handbook.md"},
    ]}
    for i, name in ((4, "policy"), (5, "handbook")):
        r[f"drives/{_D}/items/f{i}"] = {
            "id": f"f{i}", "name": f"{name}.md", "size": 60,
            "webUrl": f"https://t.sharepoint.com/sites/x/{name}.md",
            "lastModifiedDateTime": "2026-01-01T00:00:00Z",
            "@microsoft.graph.downloadUrl":
                f"https://t.sharepoint.com/dl/{name}.md"}
    return r


def _sp_crawl(sp_env, monkeypatch):
    """Crawl the subtree fixture; returns the site connect dict."""
    from _fake_graph import BLOBS as _B
    from _fake_graph import FakeGraph
    _B["https://t.sharepoint.com/dl/policy.md"] = \
        b"# Policy\n\nApprovals need CAB sign-off.\n"
    _B["https://t.sharepoint.com/dl/handbook.md"] = \
        b"# Handbook\n\nProduction deploys happen on Thursdays.\n"
    sp_env._clients["https://t.sharepoint.com/sites/x"] = (
        "fake-e2e-token", FakeGraph(_routes_with_subtree()))
    c = sp_env.connect("site:" + SITE_ID)
    job = sp_env.start_crawl(c["scope_handle"])
    r = sp_env.crawl_step(c["scope_handle"], job["job_id"])
    while r["state"] == "running":
        r = sp_env.crawl_step(c["scope_handle"], job["job_id"])
    assert r["state"] == "done"
    return c


def test_descendent_item_scope_answers_from_ancestor(sp_env, monkeypatch):
    """#240: connect(item:folder ⊂ indexed site) aliases reads — subtree
    evidence only, no re-crawl."""
    from _fake_graph import DRIVE_ID
    _sp_crawl(sp_env, monkeypatch)
    c = sp_env.connect(f"item:{DRIVE_ID}:f1")
    assert c.get("covered_by") == f"site:{SITE_ID}"
    assert c["cached"] is True and c["cached_pages"] == 2
    res = sp_env.search(c["scope_handle"], "deploys")
    assert res["evidence"]
    subtree = {f"file:{DRIVE_ID}:f4", f"file:{DRIVE_ID}:f5"}
    assert {e["page_id"] for e in res["evidence"]} <= subtree
    with pytest.raises(SecurityException):
        sp_env.get_evidence(c["scope_handle"], page_id=f"file:{DRIVE_ID}:f2")


def test_descendent_alias_survives_restart(sp_env, monkeypatch):
    from _fake_graph import DRIVE_ID
    _sp_crawl(sp_env, monkeypatch)
    c = sp_env.connect(f"item:{DRIVE_ID}:f1")
    sp_env._scopes.clear()
    sp_env._evidence_sets.clear()
    res = sp_env.search(c["scope_handle"], "deploys")
    assert res["evidence"]
    assert {e["page_id"] for e in res["evidence"]} <= \
        {f"file:{DRIVE_ID}:f4", f"file:{DRIVE_ID}:f5"}


def test_descendent_materializes_on_start_crawl(sp_env, monkeypatch):
    from _fake_graph import DRIVE_ID
    _sp_crawl(sp_env, monkeypatch)
    c = sp_env.connect(f"item:{DRIVE_ID}:f1")
    job = sp_env.start_crawl(c["scope_handle"])
    r = sp_env.crawl_step(c["scope_handle"], job["job_id"])
    while r["state"] == "running":
        r = sp_env.crawl_step(c["scope_handle"], job["job_id"])
    assert r["state"] == "done"
    assert "reads_from" not in sp_env._scopes[c["scope_handle"]]
    res = sp_env.search(c["scope_handle"], "deploys")
    assert {e["page_id"] for e in res["evidence"]} <= \
        {f"file:{DRIVE_ID}:f4", f"file:{DRIVE_ID}:f5"}


def test_descendent_alias_needs_same_principal(sp_env, monkeypatch):
    """#240 security: an index from another account never aliases."""
    from _fake_graph import DRIVE_ID
    c = _sp_crawl(sp_env, monkeypatch)
    a = sp_env._scopes[c["scope_handle"]]["key"]
    sp_env._index_for(a).meta_set("principal", "other-user")
    c2 = sp_env.connect(f"item:{DRIVE_ID}:f1")
    assert "covered_by" not in c2 and c2["cached"] is False


def test_stale_watcher_exits_on_version_skew(sp_env, monkeypatch):
    """#272: installed version diverging from the loaded one for two
    polls → the server exits so the client respawns fresh code."""
    import time

    from sharepoint_knowledge import server_sp
    calls = []

    def fake_version():
        calls.append(1)
        return "1.0" if len(calls) == 1 else "9.9"

    monkeypatch.setenv("KNOWLEDGE_PROXY_POLL_S", "0.02")
    monkeypatch.setattr(server_sp, "_installed_version", fake_version)
    monkeypatch.setattr(server_sp, "_WATCHER_STARTED", None)
    exits = []
    monkeypatch.setattr(server_sp.os, "_exit", lambda c: exits.append(c))
    server_sp._start_stale_watcher()
    end = time.monotonic() + 3
    while not exits and time.monotonic() < end:
        time.sleep(0.02)
    assert exits and exits[0] == 0 and server_sp._STALE_CODE
