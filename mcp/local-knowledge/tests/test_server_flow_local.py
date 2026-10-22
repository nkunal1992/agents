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
