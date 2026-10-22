"""Per-snapshot search cache: equivalence with the legacy per-query
read path, plus epoch-based invalidation (issue #159)."""

import types

import numpy as np

from confluence_knowledge.index import Index
from confluence_knowledge.retrieval import (
    TOP_K_RETRIEVE,
    _cosine,
    _sparse_rank,
    retrieve,
)


def _mk_chunk(pid, i, text):
    return types.SimpleNamespace(chunk_id=f"{pid}:{i}", heading_path="",
                                 kind="p", ordinal=i, text=text)


def _build_index(tmp_path, n_chunks=3000, dim=8, seed=0):
    """3 pages × n_chunks chunks with deterministic vectors/sparse."""
    rng = np.random.RandomState(seed)
    idx = Index(tmp_path / "c.db")
    words = ["alpha", "beta", "gamma", "delta", "omega"]
    for p in range(3):
        pid = f"p{p}"
        chunks, vecs, sparse = [], [], []
        for i in range(n_chunks):
            chunks.append(_mk_chunk(pid, i, f"{words[i % 5]} text {i}"))
            vecs.append(rng.rand(dim))
            sparse.append({(i * 7 + p) % 1000: float(rng.rand() + 0.01)})
        idx.upsert_page({"page_id": pid, "title": f"Page {p}", "url": "u",
                         "level": 0, "version": "1", "content_hash": "h"},
                        chunks, np.asarray(vecs, dtype=np.float32), sparse)
    idx.publish({f"p{p}" for p in range(3)})
    return idx


def _legacy_fused(idx, qvec, q_sparse):
    """Reproduce the pre-cache ranking path from the public methods."""
    chunks = idx.active_chunks()
    vecs = idx.vectors_for([c["chunk_id"] for c in chunks])
    dense = [cid for cid, _ in _cosine(qvec, vecs)][:TOP_K_RETRIEVE]
    ids = {c["chunk_id"] for c in chunks}
    fts = [cid for cid, _ in idx.fts_search("alpha beta", active=ids)
           ][:TOP_K_RETRIEVE]
    docs = idx.sparse_for([c["chunk_id"] for c in chunks])
    df, n_docs = idx.term_stats()
    sp = [cid for cid, _ in _sparse_rank(q_sparse, docs, df, n_docs)
          ][:TOP_K_RETRIEVE]
    rrf = {}
    for leg in (dense, fts, sp):
        for rank, cid in enumerate(leg):
            rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60 + rank + 1)
    return sorted(rrf, key=lambda c: -rrf[c])[:TOP_K_RETRIEVE]


class _Emb:
    def __init__(self, v):
        self._v = v

    def embed(self, texts, query: bool = False):
        return np.tile(self._v, (len(texts), 1))


class _Sparse:
    def __init__(self, q):
        self._q = q

    def query_embed(self, texts):
        return [self._q for _ in texts]


def test_cached_path_matches_legacy_ranking(tmp_path):
    idx = _build_index(tmp_path)
    rng = np.random.RandomState(7)
    qvec = rng.rand(8).astype(np.float32)
    q_sparse = {3: 0.5, 100: 0.9}
    want = _legacy_fused(idx, qvec, q_sparse)
    ev = retrieve(idx, "alpha beta", _Emb(qvec),
                  sparse=_Sparse(q_sparse))
    got = [e["chunk_id"] for e in ev]
    assert got == want[:len(ev)]
    assert len(ev) > 0


def test_search_cache_rebuilt_only_on_publish(tmp_path):
    idx = _build_index(tmp_path, n_chunks=10)
    c1 = idx.search_cache()
    assert idx.search_cache() is c1              # same epoch → same object
    idx.upsert_page({"page_id": "new", "title": "t", "url": "u",
                     "level": 0, "version": "1", "content_hash": "h"},
                    [_mk_chunk("new", 0, "added")], None)
    idx.publish({"p0", "p1", "p2", "new"})
    c2 = idx.search_cache()
    assert c2 is not c1 and "new:0" in c2.ids


def test_drop_search_cache(tmp_path):
    idx = _build_index(tmp_path, n_chunks=10)
    c1 = idx.search_cache()
    idx.drop_search_cache()
    assert idx.search_cache() is not c1


def test_chunks_by_ids_and_page_chunks(tmp_path):
    idx = _build_index(tmp_path, n_chunks=10)
    got = idx.chunks_by_ids(["p1:3", "p0:0", "missing"])
    assert [c["chunk_id"] for c in got] == ["p1:3", "p0:0"]
    pcs = idx.page_chunks("p2")
    assert [c["ordinal"] for c in pcs] == list(range(10))


def test_clear_scope_invalidates_cache(tmp_path):
    idx = _build_index(tmp_path, n_chunks=10)
    assert idx.search_cache().ids
    idx.clear_scope()
    assert idx.search_cache().ids == []
