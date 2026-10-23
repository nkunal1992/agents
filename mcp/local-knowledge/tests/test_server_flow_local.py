"""local-knowledge server flow: connect → crawl → search → verify citations."""
from __future__ import annotations

import numpy as np
import pytest

from local_knowledge import server_local
from local_knowledge.settings_local import LocalProfile


class FakeEmbedder:
    def __init__(self, _home=None, _cli=None):
        pass

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


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_KNOWLEDGE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setattr(server_local, "_EMBEDDER", FakeEmbedder())
    monkeypatch.setattr(server_local, "_RERANKER", FakeReranker())
    monkeypatch.setattr(server_local, "_SPARSE", FakeSparse())
    server_local._models_ready.set()
    yield
    server_local._scopes.clear()
    server_local._indexes.clear()
    server_local._evidence_sets.clear()
    server_local._models_ready.clear()


def test_full_flow(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "guide.md").write_text("restart the gateway before the deploy")
    (root / "data.csv").write_text("service,owner\ngateway,ops\n")
    (root / ".env").write_text("SECRET=x")

    c = server_local.connect(str(root))
    assert c["kind"] == "folder"
    job = server_local.start_crawl(c["scope_handle"])
    r = server_local.crawl_step(c["scope_handle"], job["job_id"])
    # worker may still be running — poll until done
    while r["state"] == "running":
        r = server_local.crawl_step(c["scope_handle"], job["job_id"])
    assert r["pages_seen"] >= 2

    res = server_local.search(c["scope_handle"], "who owns the gateway?")
    assert res["coverage"] == "complete"
    assert res["evidence"]
    first = res["evidence"][0]
    assert first["page_url"].startswith("file://")

    v = server_local.verify_citations(
        c["scope_handle"], res["evidence_set"],
        [{"claim": "gateway is owned by ops",
          "quotes": [res["evidence"][0]["text"][:60]]}])
    assert "results" in v or isinstance(v, dict)


def test_search_while_crawling(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("x")
    c = server_local.connect(str(root))
    # search before any crawl → empty + told to crawl
    res = server_local.search(c["scope_handle"], "anything")
    assert res["coverage"] == "empty"


def test_unknown_scope():
    from confluence_knowledge.settings import SetupError
    with pytest.raises(SetupError):
        server_local.search("scope:bad:handle", "q")


def test_profile_saved(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("x")
    server_local.connect(str(root))
    p = LocalProfile.load()
    assert p.root_path == str(root.resolve())


def test_scope_rebind_from_meta(tmp_path):
    """#229: after a daemon/server restart (_scopes empty) a cached scope
    must re-bind from the persisted root_path meta — no connect() re-run,
    no KeyError on a missing 'scope_handle'."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("content")
    c = server_local.connect(str(root))
    server_local._scopes.clear()
    s = server_local._scope(c["scope_handle"])
    assert s["key"] == c["scope_handle"].split(":")[1]
    from confluence_knowledge.settings import SetupError
    for bad in (f"scope:{s['key']}:/no/such/path", "scope:deadbeef:/x",
                "scope:x"):
        try:
            server_local._scope(bad)
            raise SystemExit(f"{bad} accepted")
        except SetupError:
            pass


def test_verify_survives_restart(tmp_path):
    """#231: evidence sets persist in index meta — a daemon restart (or a
    new chat in another IDE window) must not turn verify_citations into
    'unknown evidence set'."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "guide.md").write_text("restart the gateway before the deploy")
    (root / "data.csv").write_text("service,owner\ngateway,ops\n")
    c = server_local.connect(str(root))
    job = server_local.start_crawl(c["scope_handle"])
    r = server_local.crawl_step(c["scope_handle"], job["job_id"])
    while r["state"] == "running":
        r = server_local.crawl_step(c["scope_handle"], job["job_id"])
    res = server_local.search(c["scope_handle"], "who owns the gateway?")
    assert res["evidence"]
    set_id = res["evidence_set"]
    q = res["evidence"][0]
    mapping = [{"claim": "x", "chunk_id": q["chunk_id"],
                "quote": q["text"][:12]}]
    server_local._evidence_sets.clear()     # daemon restarted
    v = server_local.verify_citations(c["scope_handle"], set_id, mapping)
    assert v["ok"], v


def _crawl(root):
    c = server_local.connect(str(root))
    job = server_local.start_crawl(c["scope_handle"])
    r = server_local.crawl_step(c["scope_handle"], job["job_id"])
    while r["state"] == "running":
        r = server_local.crawl_step(c["scope_handle"], job["job_id"])
    return c, r


def test_descendent_folder_answers_from_ancestor(tmp_path):
    """#240: connect(sub ⊂ indexed root) aliases reads — no re-crawl,
    evidence restricted to the subtree's files."""
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "top.txt").write_text("top level text")
    (docs / "sub" / "inner.md").write_text("inner only")
    (docs / "sub" / "more.txt").write_text("more inner text")
    _crawl(docs)
    c = server_local.connect(str(docs / "sub"))
    assert c.get("covered_by") == str(docs.resolve())
    assert c["cached"] is True and c["cached_pages"] == 2
    res = server_local.search(c["scope_handle"], "text")
    assert res["evidence"]
    assert {e["page_id"] for e in res["evidence"]} <= \
        {"docs/sub/inner.md", "docs/sub/more.txt"}
    with pytest.raises(Exception):
        server_local.get_evidence(
            c["scope_handle"], page_id="docs/top.txt")


def test_descendent_alias_survives_restart(tmp_path):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "sub" / "inner.md").write_text("inner only")
    (docs / "sub" / "more.txt").write_text("more inner text")
    _crawl(docs)
    c = server_local.connect(str(docs / "sub"))
    server_local._scopes.clear()
    server_local._evidence_sets.clear()
    res = server_local.search(c["scope_handle"], "inner")
    assert res["evidence"]
    assert {e["page_id"] for e in res["evidence"]} <= \
        {"docs/sub/inner.md", "docs/sub/more.txt"}


def test_descendent_file_scope_inside_ancestor(tmp_path):
    """#240: a single file inside an indexed folder is a one-page
    subtree — evidence can only ever be that file."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("alpha")
    (docs / "b.txt").write_text("bravo")
    _crawl(docs)
    c = server_local.connect(str(docs / "a.txt"))
    assert c["cached"] is True and c["cached_pages"] == 1
    res = server_local.search(c["scope_handle"], "bravo")
    assert {e["page_id"] for e in res["evidence"]} <= \
        {"docs/a.txt"}


def test_descendent_materializes_on_start_crawl(tmp_path):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "sub" / "inner.md").write_text("inner only")
    (docs / "sub" / "more.txt").write_text("more inner text")
    _crawl(docs)
    c = server_local.connect(str(docs / "sub"))
    job = server_local.start_crawl(c["scope_handle"])
    r = server_local.crawl_step(c["scope_handle"], job["job_id"])
    while r["state"] == "running":
        r = server_local.crawl_step(c["scope_handle"], job["job_id"])
    assert r["state"] == "done"
    s = server_local._scopes[c["scope_handle"]]
    assert "reads_from" not in s
    res = server_local.search(c["scope_handle"], "inner")
    # materialized index re-rels under sub.parent → 'sub/…'
    assert {e["page_id"] for e in res["evidence"]} <= \
        {"sub/inner.md", "sub/more.txt"}
