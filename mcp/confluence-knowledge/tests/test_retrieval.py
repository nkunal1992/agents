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
