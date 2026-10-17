"""Sparse leg + model marker: payload round-trip, RRF fusion, stale detection."""

import numpy as np
import pytest

from confluence_knowledge import retrieval
from confluence_knowledge.index import Index


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
    idx.set_model_id("bge-m3-onnx-quantized")
    assert idx.model_id() == "bge-m3-onnx-quantized"
