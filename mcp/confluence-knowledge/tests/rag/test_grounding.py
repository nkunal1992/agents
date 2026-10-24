"""Confident-garbage + error-UX symptoms: noise must never ground an
answer, and every failure mode must surface a named remediation."""

import pytest

from confluence_knowledge import retrieval
from rag.conftest import HashEmbedder, ToySparse
from rag.corpus import CORPUS, NO_ANSWER


def test_irrelevant_query_yields_no_evidence(rag_index):
    """Below the rerank floor a query must return NOTHING — citing 'least
    bad' noise is the confident-garbage symptom."""
    emb, sp = HashEmbedder(), ToySparse()

    class ZeroReranker:
        def score(self, q, docs):
            import numpy as np
            return np.zeros(len(docs), dtype=np.float32)

    for q in NO_ANSWER:
        ev = retrieval.retrieve(rag_index, q, emb, ZeroReranker(), sp)
        assert ev == [], f"'{q}' produced evidence: {ev}"


def test_marginal_hits_flagged_not_dropped(rag_index):
    """Below-confidence but above-noise hits are returned with
    low_confidence=True — a hard floor deleted real answers (eval miss)."""
    emb, sp = HashEmbedder(), ToySparse()

    class HalfReranker:
        def score(self, q, docs):
            import numpy as np
            return np.full(len(docs), 0.2, dtype=np.float32)

    ev = retrieval.retrieve(rag_index, "password length", emb,
                            HalfReranker(), sp)
    assert ev and all(e["low_confidence"] for e in ev)


def test_noise_floor_still_drops_junk(rag_index):
    """Sub-floor scores (real model gives ~0.00 on irrelevance) → empty."""
    emb, sp = HashEmbedder(), ToySparse()

    class TinyReranker:
        def score(self, q, docs):
            import numpy as np
            return np.full(len(docs), 0.01, dtype=np.float32)  # < 0.1 floor

    ev = retrieval.retrieve(rag_index, "password length", emb,
                            TinyReranker(), sp)
    assert ev == []


def test_real_model_no_answer_queries(tmp_path, real_models):
    """On the real stack: no-answer queries must return empty evidence."""
    from rag.conftest import build_index
    emb, rr, sp = real_models
    idx = build_index(tmp_path, CORPUS, embedder=emb, sparse=sp)
    misses = []
    for q in NO_ANSWER:
        ev = retrieval.retrieve(idx, q, emb, rr, sp)
        if ev:
            misses.append(q)
    assert not misses, misses


def test_oversized_index_refuses_silently_growing(tmp_path):
    """>MAX_SEARCHABLE_CHUNKS must refuse with a named error, not OOM."""
    from types import SimpleNamespace

    from confluence_knowledge.settings import SetupError
    # fake a 100k+ chunk index without materializing chunk dicts — the
    # retrieval cache's id list alone proves oversize
    cache = SimpleNamespace(
        ids=[f"c{i}" for i in range(retrieval.MAX_SEARCHABLE_CHUNKS + 1)])
    class FakeIdx:
        def search_cache(self): return cache
        def ann_available(self): return False   # no ext → still refuses
    with pytest.raises(SetupError):
        retrieval.retrieve(FakeIdx(), "q", HashEmbedder())


def test_unknown_scope_and_dead_job_errors():
    """Tool-facing error UX: unknown handle → SetupError not KeyError."""
    from confluence_knowledge import server
    from confluence_knowledge.settings import SetupError
    server._scopes.clear()
    with pytest.raises(SetupError):
        server.crawl_step("scope:garbage:xyz", "job1")


def test_search_on_empty_index_reports(tmp_path, monkeypatch):
    """search() on a scope with no pages returns coverage + note."""
    from confluence_knowledge import server
    server._scopes.clear()
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path))
    s = {"key": "abc123", "profile": None, "root": "r"}
    server._scopes["scope:abc:r"] = s
    monkeypatch.setattr(server, "_index_for",
                        lambda k: __import__("confluence_knowledge.index",
                                             fromlist=["Index"])
                        .Index(tmp_path / "e.db"))
    r = server.search("scope:abc:r", "question")
    assert r["evidence"] == [] and r["coverage"] in ("empty", "crawling")
    assert "note" in r
