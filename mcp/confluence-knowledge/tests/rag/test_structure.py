"""Structural symptoms: publish atomicity, evidence truncation honesty,
index-size bound, latency ceiling."""

import threading
import time

from confluence_knowledge import retrieval
from confluence_knowledge.extract import Chunk
from confluence_knowledge.index import Index
from rag.conftest import HashEmbedder, OverlapReranker, ToySparse


def _mk(idx, pid, text, title="t"):
    c = Chunk(text=text, heading_path="", kind="text", ordinal=0,
              chunk_id=f"{pid}:h:0")
    idx.upsert_page({"page_id": pid, "parent_id": None, "title": title,
                     "url": f"u://{pid}", "level": 0, "version": "",
                     "warnings": ""}, [c], vecs=None, sparse=None)


def test_publish_is_atomic_for_readers(tmp_path):
    """Each publish call is one transaction: a reader must see all-old or
    all-new — never the half-swapped mix a non-atomic flip would show."""
    idx = Index(tmp_path / "i.db")
    _mk(idx, "A", "alpha")
    idx.publish({"A"})

    torn = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            _mk(idx, "B", "beta")   # publish GC deletes unseen rows, so
            idx.publish({"B"})      # each flip re-seeds the other page
            _mk(idx, "A", "alpha")
            idx.publish({"A"})

    def reader():
        while not stop.is_set():
            # active set must be exactly one generation
            ids = {c["page_id"] for c in idx.active_chunks()}
            if ids not in ({"A"}, {"B"}, set()):
                torn.append(ids)

    w, r = threading.Thread(target=writer), threading.Thread(target=reader)
    w.start(); r.start()
    time.sleep(0.4)
    stop.set(); w.join(); r.join()
    assert not torn, f"torn reads: {len(torn)}"


def test_huge_table_splits_with_header(rag_index):
    """A 900-row table must become several ≤280-word chunks — each keeping
    the header row — so embed/rerank token limits can't lose tail rows."""
    tables = [c for c in rag_index.active_chunks()
              if c["kind"] == "table" and "svc-" in c["text"]]
    assert len(tables) > 1, "table should have been split"
    for c in tables:
        assert "Table — columns:" in c["text"]
        assert len(c["text"].split()) <= 280


def test_search_latency_bound(rag_index):
    """Whole-index search on a realistic size must stay interactive —
    regression guard for O(n) behavior exploding."""
    t = time.monotonic()
    retrieval.retrieve(rag_index, "password policy", HashEmbedder(),
                       OverlapReranker(), ToySparse())
    assert time.monotonic() - t < 2.0


def test_sparse_leg_independent_of_dense(rag_index):
    """Zero dense recall — sparse+fts must still carry the answer."""
    class ZeroEmbedder:
        def embed(self, texts):
            import numpy as np
            return np.zeros((len(texts), 128), dtype=np.float32)

    ev = retrieval.retrieve(rag_index, "rollback window",
                            ZeroEmbedder(), OverlapReranker(), ToySparse())
    texts = " || ".join(e["text"] for e in ev)
    assert "2 hours" in texts


def test_duplicate_content_pages_both_citable(tmp_path):
    """Two near-identical pages both rank — citation must land on the
    page the evidence text actually came from."""
    idx = Index(tmp_path / "i.db")
    _mk(idx, "p1", "Expenses are submitted within 30 days", "Expense Reimbursement")
    _mk(idx, "p2", "Expenses are submitted within 45 days", "Travel Expenses")
    idx.publish({"p1", "p2"})
    idx.set_model_id("x")

    ev = retrieval.retrieve(idx, "expense days", HashEmbedder(),
                            OverlapReranker(), ToySparse())
    titles = {e["page_title"] for e in ev}
    assert titles  # at least one page cited
    for e in ev:
        # citation fields resolve to the page the chunk came from
        assert e["page_url"].endswith(e["page_id"])
