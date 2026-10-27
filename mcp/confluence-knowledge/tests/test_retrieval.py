"""Retrieval pipeline with stubbed local models — no network, no torch."""

import numpy as np

from confluence_knowledge.extract import Chunk
from confluence_knowledge.index import Index
from confluence_knowledge.retrieval import TOP_K_EVIDENCE, check_citations, retrieve


class FakeEmbedder:
    """Deterministic 'embedding': hashed word bucket → fixed 8-dim vector."""

    def embed(self, texts, query: bool = False):
        out = np.zeros((len(texts), 8), np.float32)
        for i, t in enumerate(texts):
            for w in t.lower().split():
                out[i][hash(w) % 8] += 1.0
            out[i] /= (np.linalg.norm(out[i]) + 1e-9)
        return out


class FakeReranker:
    def score(self, query, docs):
        qw = set(query.lower().split())
        return np.array([len(qw & set(d.lower().split()))
                         for d in docs], np.float32)


def _index(tmp_path):
    idx = Index(tmp_path / "s.db")
    emb = FakeEmbedder()
    texts = [
        ("1", "Shipping", "releases ship on tuesdays after qa signoff"),
        ("2", "Access",   "vpn access requires manager approval"),
        ("3", "Deep",     "production rollout needs change-freeze exemption"),
    ]
    for i, (pid, title, body) in enumerate(texts):
        c = Chunk(body, "", "text", 0, f"c{pid}")
        vec = emb.embed([body])
        idx.upsert_page({"page_id": pid, "title": title,
                         "url": f"/p/{pid}", "level": i, "version": "1",
                         "content_hash": "h", "warnings": []}, [c], vec)
    idx.publish({"1", "2", "3"})
    return idx


def test_relevant_chunk_ranked(tmp_path):
    ev = retrieve(_index(tmp_path), "when do releases ship",
                  FakeEmbedder(), FakeReranker())
    assert ev and ev[0]["page_title"] == "Shipping"
    assert len(ev) <= TOP_K_EVIDENCE


def test_evidence_carries_citation_fields(tmp_path):
    ev = retrieve(_index(tmp_path), "vpn approval", FakeEmbedder())
    assert all({"page_title", "page_url", "chunk_id", "text"} <= set(e)
               for e in ev)


def test_rerank_max_env_bound():
    """#281: the rerank window is the dominant search cost — bounded by
    env, clamped to [TOP_K_EVIDENCE, 40], default 12."""
    import os

    from confluence_knowledge.retrieval import _rerank_max
    old = os.environ.pop("KNOWLEDGE_RERANK_MAX", None)
    try:
        assert _rerank_max() == 12
        os.environ["KNOWLEDGE_RERANK_MAX"] = "25"
        assert _rerank_max() == 25
        os.environ["KNOWLEDGE_RERANK_MAX"] = "garbage"
        assert _rerank_max() == 12
        os.environ["KNOWLEDGE_RERANK_MAX"] = "2"
        assert _rerank_max() == TOP_K_EVIDENCE   # never below evidence cap
        os.environ["KNOWLEDGE_RERANK_MAX"] = "999"
        assert _rerank_max() == 40               # latency guard rail
    finally:
        if old is None:
            os.environ.pop("KNOWLEDGE_RERANK_MAX", None)
        else:
            os.environ["KNOWLEDGE_RERANK_MAX"] = old


def test_identifier_leg_queries_fts_separately(tmp_path, monkeypatch):
    """#338: identifier-shaped terms get a dedicated identifier-only FTS
    pass; queries without identifiers cost nothing extra."""
    idx = _index(tmp_path)
    calls = []
    orig = idx.fts_search
    def spy(q, limit=30, active=None):
        calls.append((q, limit))
        return orig(q, limit, active)
    monkeypatch.setattr(idx, "fts_search", spy)

    retrieve(idx, "who owns service-42x", FakeEmbedder())
    assert len(calls) == 2
    assert calls[1] == ("service-42x", 10)

    calls.clear()
    retrieve(idx, "who owns the service", FakeEmbedder())
    assert len(calls) == 1            # no identifier → no extra pass


def test_identifier_hit_rides_into_fused_pool(tmp_path):
    """#338: a doc whose only query link is the identifier phrase is
    rescued into the evidence window even when common-term matches
    would bury it — the measured scale-test failure."""
    idx = Index(tmp_path / "s.db")
    emb = FakeEmbedder()
    # 12 filler docs all rich in the query's common vocabulary
    for i in range(12):
        body = ("team owns service and the freeze window — review "
                "approval rollback board pipeline signoff handover") \
               + f" filler{i}"
        idx.upsert_page({"page_id": str(i), "title": f"F{i}",
                         "url": f"/p/{i}", "level": 0, "version": "1",
                         "content_hash": "h", "warnings": []},
                        [Chunk(body, "", "text", 0, f"c{i}")],
                        emb.embed([body]))
    # the needle: carries the identifier but almost no query vocabulary
    body = "unrelated prose entirely zzz " \
           "the freeze owner for service-42x is team-9"
    idx.upsert_page({"page_id": "n", "title": "Needle",
                     "url": "/p/needle", "level": 0, "version": "1",
                     "content_hash": "h", "warnings": []},
                    [Chunk(body, "", "text", 0, "cn")],
                    emb.embed([body]))
    idx.publish({str(i) for i in range(12)} | {"n"})
    ev = retrieve(idx, "which team owns service-42x and the freeze window",
                  FakeEmbedder())
    ids = [e["page_id"] for e in ev]
    assert "n" in ids[:4]          # identifier leg lifts it to the top


def test_citation_check(tmp_path):
    ev = retrieve(_index(tmp_path), "vpn approval", FakeEmbedder())
    cid = ev[0]["chunk_id"]
    ok = check_citations(ev, [{"claim": "x", "chunk_id": cid,
                               "quote": "manager approval"}])
    assert ok["ok"]
    bad = check_citations(ev, [{"claim": "x", "chunk_id": "nope",
                                "quote": "y"}])
    assert not bad["ok"]
    bad2 = check_citations(ev, [{"claim": "x", "chunk_id": cid,
                                 "quote": "not in the text"}])
    assert not bad2["ok"]


def test_sparse_embedder_transitive_dep_gap(tmp_path, monkeypatch):
    """#287: a missing transitive dep (py_rust_stemmers on win_arm64)
    raises SparseUnavailable — degradable — while fastembed itself
    missing stays a hard SetupError on platforms where it's expected
    (the #302 marker-skipped case is covered separately)."""
    import builtins

    import pytest

    from confluence_knowledge import retrieval
    from confluence_knowledge.retrieval import LocalSparseEmbedder, SparseUnavailable
    from confluence_knowledge.settings import SetupError

    monkeypatch.setattr(retrieval, "sparse_platform_excluded",
                        lambda: False)
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "fastembed":
            raise ModuleNotFoundError(
                "No module named 'py_rust_stemmers'",
                name="py_rust_stemmers")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SparseUnavailable) as ei:
        LocalSparseEmbedder(tmp_path)
    assert "py_rust_stemmers" in str(ei.value)

    def fake_import2(name, *a, **k):
        if name == "fastembed":
            raise ModuleNotFoundError("No module named 'fastembed'",
                                      name="fastembed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import2)
    with pytest.raises(SetupError) as ei2:
        LocalSparseEmbedder(tmp_path)
    assert not isinstance(ei2.value, SparseUnavailable)
    assert "isn't installed" in str(ei2.value)


def test_sparse_platform_excluded_marker_match(monkeypatch):
    """#302: the predicate must mirror the pip marker in pyproject.toml —
    win32 + ARM64 machine is where fastembed gets skipped at install."""
    import platform
    import sys

    from confluence_knowledge import retrieval

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform, "machine", lambda: "ARM64")
    assert retrieval.sparse_platform_excluded() is True
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    assert retrieval.sparse_platform_excluded() is False
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    assert retrieval.sparse_platform_excluded() is False


def test_sparse_embedder_missing_on_excluded_platform_degrades(
        tmp_path, monkeypatch):
    """#302: where the marker skips fastembed at install (win_arm64),
    its absence is the expected wheel gap — SparseUnavailable, not the
    'reinstall' SetupError that would loop the user forever."""
    import builtins

    import pytest

    from confluence_knowledge import retrieval
    from confluence_knowledge.retrieval import LocalSparseEmbedder, SparseUnavailable

    monkeypatch.setattr(retrieval, "sparse_platform_excluded",
                        lambda: True)

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "fastembed":
            raise ModuleNotFoundError("No module named 'fastembed'",
                                      name="fastembed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SparseUnavailable):
        LocalSparseEmbedder(tmp_path)


def test_load_sparse_or_none_degrades(tmp_path, monkeypatch):
    """#320: harnesses/eval share the degrade via load_sparse_or_none —
    None on SparseUnavailable, SetupError still propagates."""
    import builtins

    import pytest

    from confluence_knowledge import retrieval

    monkeypatch.setattr(retrieval, "sparse_platform_excluded",
                        lambda: True)
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "fastembed":
            raise ModuleNotFoundError("No module named 'fastembed'",
                                      name="fastembed")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert retrieval.load_sparse_or_none(tmp_path) is None

    # a hard failure still propagates — never silently degraded
    def boom(*a, **k):
        raise retrieval.SetupError("corrupt install")
    monkeypatch.setattr(retrieval, "LocalSparseEmbedder", boom)
    with pytest.raises(retrieval.SetupError):
        retrieval.load_sparse_or_none(tmp_path)
