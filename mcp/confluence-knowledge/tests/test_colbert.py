"""ColBERT late-interaction path (#329) — stubbed scorer, real Index."""

import threading

import numpy as np

from confluence_knowledge.extract import Chunk
from confluence_knowledge.index import Index
from confluence_knowledge.retrieval import backfill_tokvecs, retrieve


class FakeEmbedder:
    def embed(self, texts, query: bool = False):
        out = np.zeros((len(texts), 8), np.float32)
        for i, t in enumerate(texts):
            for w in t.lower().split():
                out[i][hash(w) % 8] += 1.0
            out[i] /= (np.linalg.norm(out[i]) + 1e-9)
        return out


SIG = "fake-model@r1:96"


class Verifier:
    """Stands in for the cross-encoder — `prob` is returned for any pair,
    so tests control whether the top-1 abstention check passes."""

    def __init__(self, prob: float = 0.9):
        self.prob = prob

    def score(self, query, docs):
        return np.full(len(docs), self.prob, np.float32)


class FakeColbert:
    """Score is read back out of the stored mat's first cell — tests pin
    per-candidate scores through put_tokvecs/encode_docs."""

    def __init__(self):
        self.sig = SIG
        self.last = {}
        self.encode_calls = 0

    def score(self, query, doc_mats):
        return np.array([float(dm.flat[0]) if dm.size else 0.0
                         for dm in doc_mats], np.float32)

    def encode_docs(self, texts):
        self.encode_calls += 1
        # deterministic per-text score baked into the mat
        return [np.full((4, 96), 0.9 - 0.05 * i, np.float16)
                for i, _t in enumerate(texts)]


def _mat(score: float) -> np.ndarray:
    return np.full((2, 96), score, np.float16)


def _index(tmp_path, bodies=(("1", "Shipping", "releases ship on tuesdays"),
                             ("2", "Access", "vpn access needs approval"),
                             ("3", "Deep", "rollout needs freeze exemption"))):
    idx = Index(tmp_path / "s.db")
    emb = FakeEmbedder()
    for i, (pid, title, body) in enumerate(bodies):
        c = Chunk(body, "", "text", 0, f"c{pid}")
        idx.upsert_page({"page_id": pid, "title": title,
                         "url": f"/p/{pid}", "level": i, "version": "1",
                         "content_hash": "h", "warnings": []}, [c],
                        emb.embed([body]))
    idx.publish({b[0] for b in bodies})
    return idx


# ---- storage -----------------------------------------------------------

def test_tokvecs_roundtrip(tmp_path):
    idx = _index(tmp_path)
    idx.put_tokvecs({"c1": _mat(0.9), "c2": _mat(0.5)}, sig=SIG)
    mats = idx.tokvecs_for(["c1", "c2", "c3", "missing"], SIG)
    assert set(mats) == {"c1", "c2"}
    assert mats["c1"].shape == (2, 96) and mats["c1"].dtype == np.float32
    assert abs(float(mats["c1"].flat[0]) - 0.9) < 1e-3


def test_tokvecs_sig_mismatch_reads_as_uncovered(tmp_path):
    """A colbert upgrade (different sig) must not score against vectors
    a different projection wrote — they read as uncovered and the
    backfill re-encodes."""
    idx = _index(tmp_path)
    idx.put_tokvecs({"c1": _mat(0.9)}, sig="old-model@r0:96")
    assert idx.tokvecs_for(["c1"], SIG) == {}
    assert {m[0] for m in idx.tokvecs_missing(10, SIG)} == {"c1", "c2", "c3"}
    idx.put_tokvecs({"c1": _mat(0.9), "c2": _mat(0.8)}, sig=SIG)
    assert idx.tokvec_sig() == SIG
    assert set(idx.tokvecs_for(["c1"], SIG)) == {"c1"}
    # rows under the old sig were dropped, not just shadowed
    assert {m[0] for m in idx.tokvecs_missing(10, SIG)} == {"c3"}


def test_tokvecs_missing_and_gc(tmp_path):
    idx = _index(tmp_path)
    assert len(idx.tokvecs_missing(10)) == 3
    idx.put_tokvecs({"c1": _mat(0.9)}, sig=SIG)
    missing = idx.tokvecs_missing(10)
    assert {m[0] for m in missing} == {"c2", "c3"}
    # orphan row (no active chunk) is swept
    idx.put_tokvecs({"ghost": _mat(0.9)}, sig=SIG)
    assert idx.gc_tokvecs() == 1
    assert "ghost" not in idx.tokvecs_for(["ghost"], SIG)


def test_publish_drops_tokvecs_for_removed_pages(tmp_path):
    idx = _index(tmp_path)
    idx.put_tokvecs({"c3": _mat(0.9)}, sig=SIG)
    # republish without page 3 — its chunks are confirmed removals
    idx.publish({"1", "2"})
    assert "c3" not in idx.tokvecs_for(["c3"], SIG)


# ---- backfill ----------------------------------------------------------

def test_backfill_encodes_missing_then_noops(tmp_path):
    idx = _index(tmp_path)
    cb = FakeColbert()
    assert backfill_tokvecs(idx, lambda: cb) == 3
    assert idx.tokvecs_missing(10, SIG) == []
    assert idx.tokvec_sig() == SIG
    assert backfill_tokvecs(idx, lambda: cb) == 0


def test_backfill_reencodes_on_sig_change(tmp_path):
    """Upgrade path: rows stamped under another model are re-encoded,
    not left scoring stale projections."""
    idx = _index(tmp_path)
    idx.put_tokvecs({c: _mat(0.9) for c in ("c1", "c2", "c3")},
                    sig="old-model@r0:96")
    cb = FakeColbert()
    assert backfill_tokvecs(idx, lambda: cb) == 3
    assert idx.tokvec_sig() == SIG
    assert idx.tokvecs_missing(10, SIG) == []


def test_backfill_degraded_factory(tmp_path):
    idx = _index(tmp_path)
    assert backfill_tokvecs(idx, lambda: None) == 0
    assert len(idx.tokvecs_missing(10)) == 3   # untouched — cross-encoder covers


# ---- retrieve() --------------------------------------------------------

def test_colbert_orders_and_marks_scorer(tmp_path):
    idx = _index(tmp_path)
    idx.put_tokvecs({"c1": _mat(0.70), "c2": _mat(0.95), "c3": _mat(0.90)},
                    sig=SIG)
    # union evidence is displayed in fused rank order (#336)
    fused = [e["chunk_id"] for e in
             retrieve(idx, "vpn releases", FakeEmbedder())]
    ev = retrieve(idx, "vpn releases", FakeEmbedder(), Verifier(0.9),
                  colbert=FakeColbert())
    assert [e["chunk_id"] for e in ev] == fused[:8]
    assert all(e["scorer"] == "colbert" for e in ev)
    by_id = {e["chunk_id"]: e for e in ev}
    # c1 is above the sanity floor but under the weak-tail mark
    assert by_id["c1"]["low_confidence"] is True
    assert by_id["c2"]["low_confidence"] is False


def test_colbert_abstains_when_top_pick_fails_floor(tmp_path):
    """The cross-encoder verifies the top late-interaction pick — below
    MIN_RERANK_SCORE the whole set is junk and nothing surfaces."""
    idx = _index(tmp_path)
    idx.put_tokvecs({c: _mat(0.95) for c in ("c1", "c2", "c3")}, sig=SIG)
    ev = retrieve(idx, "zzz qqq", FakeEmbedder(), Verifier(0.05),
                  colbert=FakeColbert())
    assert ev == []


def test_partial_coverage_falls_back_to_cross_encoder(tmp_path):
    """Mixed score scales can't compare — one missing tokvec sends the
    whole call through the cross-encoder."""
    idx = _index(tmp_path)
    idx.put_tokvecs({"c2": _mat(0.99)}, sig=SIG)  # 1 of 3 fused candidates
    ev = retrieve(idx, "vpn releases", FakeEmbedder(), Verifier(0.9),
                  colbert=FakeColbert())
    assert ev and all(e["scorer"] == "cross-encoder" for e in ev)


def test_colbert_without_reranker_uses_own_floor(tmp_path):
    idx = _index(tmp_path)
    idx.put_tokvecs({"c1": _mat(0.95), "c2": _mat(0.40), "c3": _mat(0.90)},
                    sig=SIG)
    ev = retrieve(idx, "vpn releases", FakeEmbedder(), reranker=None,
                  colbert=FakeColbert())
    # 0.40 < min floor drops c2; the union keeps the rest in fused order
    assert {e["chunk_id"] for e in ev} == {"c1", "c3"}


def test_union_keeps_fused_hits_colbert_ranked_out(tmp_path):
    """#336: MaxSim can push a fused-top-8 hit below the evidence cut in
    keyword-dense corpora (discriminating tokens get diluted). The union
    membership keeps fused-top-8 items even when colbert ranks them
    last — while colbert's own deep-fused picks still enter."""
    bodies = [(str(i), f"T{i}", f"topic{i} filler shared words here")
              for i in range(10)]
    idx = _index(tmp_path, bodies)
    emb = FakeEmbedder()
    top8 = [e["chunk_id"] for e in
            retrieve(idx, "topic1", emb)]          # fused top-8, no scorer
    deep = [f"c{i}" for i in range(10) if f"c{i}" not in top8]
    assert len(top8) == 8 and len(deep) == 2
    # colbert loves the two fused-deepest chunks and hates the fused-top-2
    mats = {top8[0]: _mat(0.63), top8[1]: _mat(0.64),
            deep[0]: _mat(0.99), deep[1]: _mat(0.98)}
    for cid in top8[2:]:
        mats[cid] = _mat(0.90)
    idx.put_tokvecs(mats, sig=SIG)
    ev = retrieve(idx, "topic1", emb, Verifier(0.9), colbert=FakeColbert())
    ids = [e["chunk_id"] for e in ev]
    # fused-top-2 rescued by the union although colbert ranked them last
    assert ids[:8] == top8
    # colbert's deep picks still enter evidence via its own top-8
    assert set(ids[8:]) == set(deep)


def test_no_scorers_still_returns_top_candidates(tmp_path):
    idx = _index(tmp_path)
    ev = retrieve(idx, "vpn", FakeEmbedder(), reranker=None, colbert=None)
    assert len(ev) > 0 and all(e["rerank_score"] is None for e in ev)


# ---- post-publish hook --------------------------------------------------

def test_direct_db_close_during_backfill_no_segfault(tmp_path):
    """CI segfault (#329): teardowns close idx.db directly, bypassing
    Index.close()'s warm-thread join — a POST_PUBLISH backfill writing
    on self.db mid-close is use-after-free. The backfill's dedicated
    writer keeps its work off the shared handle."""
    import confluence_knowledge.index as index_mod

    idx = _index(tmp_path)
    cb = FakeColbert()
    old = index_mod.POST_PUBLISH
    # tiny batch stretches the backfill across the close
    index_mod.POST_PUBLISH = lambda i: backfill_tokvecs(
        i, lambda: cb, batch=1)
    try:
        idx.publish({"1", "2", "3"})
        idx.db.close()          # the crash vector — direct close
    finally:
        index_mod.POST_PUBLISH = old
    for t in list(idx._warm_threads):
        t.join(timeout=10)
    # either outcome is correct — the backfill may win or legitimately
    # lose the race to the close; the contract is: no segfault and the
    # index file stays valid
    idx2 = Index(tmp_path / "s.db")
    assert idx2.stats()["pages"] == 3


def test_post_publish_hook_runs_on_warm_thread(tmp_path, monkeypatch):
    import confluence_knowledge.index as index_mod

    fired = threading.Event()
    monkeypatch.setattr(index_mod, "POST_PUBLISH", lambda i: fired.set())
    Index(tmp_path / "s.db").publish(set())
    assert fired.wait(timeout=5)


# ---- model pool degrade -------------------------------------------------

def test_pool_colbert_degrades_on_setup_error(tmp_path):
    from confluence_knowledge.models import ModelPool
    from confluence_knowledge.settings import SetupError

    pool = ModelPool(lambda: tmp_path, "test-cli")
    def boom(*_a, **_k):
        raise SetupError("colbert artifacts missing")
    pool._loaders["colbert"] = boom
    assert pool.colbert() is None
    assert "colbert" in (pool.colbert_missing or "")
    assert pool.colbert() is None          # sticky — doesn't retry per call
