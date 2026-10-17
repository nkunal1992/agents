"""Sparse leg + model marker: payload round-trip, RRF fusion, stale detection."""

import numpy as np
import pytest

from confluence_knowledge import retrieval
from confluence_knowledge.extract import Chunk
from confluence_knowledge.index import Index
from confluence_knowledge.retrieval import MODEL_MARKER


class _C:
    def __init__(self, cid, text):
        self.chunk_id = cid
        self.heading_path = ""
        self.kind = "para"
        self.ordinal = 0
        self.text = text


def _page(idx, pid, chunks, sparse=None):
    idx.upsert_page(
        {"page_id": pid, "title": pid, "url": f"u:{pid}", "level": 0},
        [_C(f"{pid}:c{i}", t) for i, t in enumerate(chunks)],
        np.ones((len(chunks), 4), dtype=np.float32),
        sparse)


def test_sparse_roundtrip(tmp_path):
    idx = Index(tmp_path / "i.db")
    _page(idx, "p1", ["alpha beta"], [{1: 0.5, 7: 1.5, 300_000: 2.0}])
    idx.publish({"p1"})
    got = idx.sparse_for(["p1:c0"])
    assert got["p1:c0"] == pytest.approx({1: 0.5, 7: 1.5, 300_000: 2.0})
    assert idx.sparse_for(["missing"]) == {}


def test_sparse_replaced_on_reindex(tmp_path):
    idx = Index(tmp_path / "i.db")
    _page(idx, "p1", ["a"], [{1: 1.0}])
    _page(idx, "p1", ["b"], [{9: 3.0}])   # re-upsert same page
    got = idx.sparse_for(["p1:c0"])
    assert got["p1:c0"] == pytest.approx({9: 3.0})


def test_sparse_leg_lifts_lexical_hit(tmp_path):
    """A chunk whose sparse terms match the query but which dense+FTS rank
    lower should surface through the sparse leg's RRF contribution."""
    idx = Index(tmp_path / "i.db")
    # dense vectors all identical → no dense discrimination; FTS can't match
    # "zyx" literally; only sparse gives the hit a score
    for pid in ("p1", "p2", "p3"):
        _page(idx, pid, ["ordinary words here"], None)
    idx.upsert_page({"page_id": "hit", "title": "t", "url": "u", "level": 0},
                    [_C("hit:c0", "ordinary words here")],
                    np.ones((1, 4), dtype=np.float32), [{42: 5.0}])
    idx.publish({"p1", "p2", "p3", "hit"})

    emb = type("E", (), {"embed": lambda s, t: np.ones((len(t), 4),
                                                       dtype=np.float32)})
    spr = type("S", (), {"embed": lambda s, t: [{42: 5.0} for _ in t]})
    ev = retrieval.retrieve(idx, "zyx query", emb(), reranker=None, sparse=spr())
    assert ev[0]["chunk_id"] == "hit:c0"


def test_sparse_leg_optional(tmp_path):
    idx = Index(tmp_path / "i.db")
    _page(idx, "p1", ["some text"])
    idx.publish({"p1"})
    emb = type("E", (), {"embed": lambda s, t: np.ones((len(t), 4),
                                                       dtype=np.float32)})
    ev = retrieval.retrieve(idx, "text", emb())   # sparse=None — still works
    assert ev and ev[0]["chunk_id"] == "p1:c0"


def test_model_marker(tmp_path):
    idx = Index(tmp_path / "i.db")
    assert idx.model_id() is None           # pre-0.5 index → no marker
    idx.set_model_id(MODEL_MARKER)
    assert idx.model_id() == MODEL_MARKER


def test_sparse_id_int64_range(tmp_path):
    """fastembed Qdrant/bm25 emits int64 hashed ids — int32 packing would
    silently wrap ids >2^31 and kill the sparse leg (C-4 hunt bug)."""
    idx = Index(tmp_path / "i.db")
    huge = 2**31 + 12345      # > int32 range — wraps negative if packed as i4
    c = Chunk(text="word", heading_path="", kind="text", ordinal=0,
              chunk_id="c1")
    p = {"page_id": "p", "parent_id": None, "title": "t", "url": "u",
         "level": 0, "version": "", "warnings": ""}
    idx.upsert_page(p, [c], vecs=None, sparse=[{huge: 1.5}])
    idx.publish({"p"})
    got = idx.sparse_for(["c1"])
    assert huge in got["c1"], "sparse id >2^31 must round-trip intact"
    assert got["c1"][huge] == pytest.approx(1.5)


def test_cosine_mixed_dims_skips(tmp_path):
    """A model swap can leave mismatched-dim blobs — retrieval must skip
    them, not crash np.stack (C-1 hunt bug)."""
    q = np.ones(8, dtype=np.float32)
    mat = {"ok": np.ones(8, dtype=np.float32),
           "stale": np.ones(4, dtype=np.float32)}
    ranked = retrieval._cosine(q, mat)
    assert [cid for cid, _ in ranked] == ["ok"]


def test_step_aborts_on_stale_model_mid_crawl(tmp_path):
    """Job checkpointed under an old model must not resume mixing dims —
    scope is wiped and the job errors so start_crawl rebuilds (C-1)."""
    idx = Index(tmp_path / "i.db")
    idx.set_model_id("old-model-marker")
    # a page already indexed under the old model
    c = Chunk(text="stale", heading_path="", kind="text", ordinal=0,
              chunk_id="c1")
    idx.upsert_page({"page_id": "p1", "parent_id": None, "title": "t",
                     "url": "u", "level": 0, "version": "", "warnings": ""},
                    [c], vecs=None, sparse=None)
    idx.create_job("j1", "root")
    idx.save_job("j1", "running", ["page:2", "page:3"], [])
    idx.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("seen:j1", '["p1"]')); idx.db.commit()

    from confluence_knowledge.crawl import Crawler
    cr = Crawler(client=None, index=idx, env="datacenter")
    r = cr.step("j1", batch=None, budget_s=None)
    assert r["state"] == "error"
    assert "model changed" in r["errors"][0]
    assert idx.model_id() is None            # marker wiped
    assert idx.active_chunks() == []         # stale content cleared
