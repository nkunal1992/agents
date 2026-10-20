"""Missed-answer symptoms: the gold chunk must be reachable through each
retrieval leg and survive fusion — fakes for logic, real models optional."""


from confluence_knowledge import retrieval
from rag.conftest import HashEmbedder, OverlapReranker, ToySparse


def _gold_chunks(idx, corpus):
    """Map each (question, gold) to whether ANY returned chunk contains it."""
    emb, rr, sp = HashEmbedder(), OverlapReranker(), ToySparse()
    misses = []
    for title, _html, qas in corpus:
        for q, gold in qas:
            ev = retrieval.retrieve(idx, q, emb, rr, sp)
            texts = " || ".join(e["text"] for e in ev)
            if gold not in texts:
                misses.append((title, q, gold))
    return misses


def test_lexical_recall_with_fakes(rag_index):
    """Keyword-shaped queries must find their gold chunk through
    fts/sparse legs even with a semantically-blind dense model."""
    from rag.corpus import CORPUS
    # skip CJK — the toy sparse tokenizer can't segment it (see CJK test)
    misses = _gold_chunks(rag_index, [(t, h, [x for x in qa
                                          if all(ord(c) < 128 for c in x[0])])
                                      for t, h, qa in CORPUS])
    assert not misses, misses


def test_real_model_recall(tmp_path, real_models):
    """Paraphrase + keyword recall on the real ONNX stack."""
    from rag.conftest import build_index
    from rag.corpus import CORPUS
    emb, rr, sp = real_models
    idx = build_index(tmp_path, CORPUS, embedder=emb, sparse=sp)
    misses = []
    for title, _html, qas in CORPUS:
        for q, gold in qas:
            ev = retrieval.retrieve(idx, q, emb, rr, sp)
            texts = " || ".join(e["text"] for e in ev)
            if gold not in texts:
                misses.append((title, q, gold))
    assert not misses, misses


def test_huge_table_tail_row_reachable(rag_index):
    """A 900-row table is one oversized chunk — its tail row must still
    be reachable (the FTS leg carries what embedding truncates)."""
    emb, rr, sp = HashEmbedder(), OverlapReranker(), ToySparse()
    ev = retrieval.retrieve(rag_index, "svc-0800 port", emb, rr, sp)
    texts = " || ".join(e["text"] for e in ev)
    assert "tcp/10800" in texts


def test_empty_index_returns_nothing(tmp_path):
    from confluence_knowledge.index import Index
    idx = Index(tmp_path / "empty.db")
    ev = retrieval.retrieve(idx, "anything", HashEmbedder(),
                            OverlapReranker(), ToySparse())
    assert ev == []


def test_long_query_tail_terms_not_dropped(rag_index):
    """FTS caps at 16 words — a query whose answer term sits at word 20
    must still surface via dense/sparse legs (documents the cap's risk)."""
    emb, rr, sp = HashEmbedder(), OverlapReranker(), ToySparse()
    q = ("please help me understand the company documentation for new "
         "employees regarding laptop pickup location floor")
    ev = retrieval.retrieve(rag_index, q, emb, rr, sp)
    texts = " || ".join(e["text"] for e in ev)
    assert "floor 3" in texts
