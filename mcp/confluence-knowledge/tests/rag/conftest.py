"""Fixtures for the RAG symptom-hunt suite.

Fakes make logic bugs deterministic (hash-bag dense vectors, overlap
reranker, toy sparse). The real-models fixture is opt-in via
RAG_REAL_MODELS=1 + a seeded model cache — never in default CI.
"""

import os
from pathlib import Path

import numpy as np
import pytest

from confluence_knowledge.index import Index


def _tokens(t: str) -> list[str]:
    """Stem-ish tokens — enough lexical signal to make retrieval non-random
    without pretending to be a model."""
    import re
    return [w[:5] for w in re.findall(r"[a-z0-9]+", t.lower())]


def _stable_hash(w: str) -> int:
    """hash() is salted per process — collision patterns would differ run
    to run, making tests flaky. md5 is stable across interpreters."""
    import hashlib
    return int.from_bytes(hashlib.md5(w.encode()).digest()[:4], "little")


class HashEmbedder:
    """Deterministic bag-of-hash dense vectors — semantically meaningless
    but stable enough to exercise the pipeline shape."""

    DIM = 128

    def embed(self, texts):
        out = np.zeros((len(texts), self.DIM), dtype=np.float32)
        for r, t in enumerate(texts):
            for w in _tokens(t):
                out[r, _stable_hash(w) % self.DIM] += 1.0
        n = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(n == 0, 1, n)


class OverlapReranker:
    """Scores = fraction of query tokens present — honest lexical signal."""

    def score(self, query, docs):
        qw = set(_tokens(query))
        # map lexical overlap into the sigmoid range the real reranker
        # produces: any positive overlap clears the 0.3 evidence floor,
        # zero overlap scores 0
        return np.array(
            [0.6 * len(qw & set(_tokens(d))) / max(len(qw), 1)
             + (0.4 if qw & set(_tokens(d)) else 0.0)
             for d in docs], dtype=np.float32)


class ToySparse:
    """Word-hash sparse embedder with the same API as LocalSparseEmbedder."""

    def embed(self, texts):
        out = []
        for t in texts:
            d: dict[int, float] = {}
            for w in _tokens(t):
                d[_stable_hash(w) % 30000] = 1.0
            out.append(d)
        return out


def build_index(tmp_path: Path, corpus, embedder=None, sparse=None) -> Index:
    """Index the corpus with fakes — mirrors what a crawler produces.

    Pass real models to build a genuinely-embedded index — without them
    the 128-dim fake vectors silently disable the dense leg of retrieve()
    (dim guard), which made test_real_model_* measure only FTS recall."""
    idx = Index(tmp_path / "eval.db")
    from confluence_knowledge.extract import extract_chunks
    emb = embedder or HashEmbedder()
    sp = sparse or ToySparse()
    for i, (title, html, _qa) in enumerate(corpus):
        pid = f"p{i}"
        chunks, _warnings = extract_chunks(html, page_title=title)
        for c in chunks:
            c.chunk_id = f"{pid}:h:{c.ordinal}"
        texts = [c.text for c in chunks]
        vecs = emb.embed(texts) if texts else None
        sparse = sp.embed(texts) if texts else None
        idx.upsert_page(
            {"page_id": pid, "parent_id": None, "title": title,
             "url": f"https://x.test/{pid}", "level": 0, "version": "",
             "warnings": ""},
            chunks, vecs=vecs, sparse=sparse)
    from confluence_knowledge.retrieval import MODEL_MARKER
    idx.publish({f"p{i}" for i in range(len(corpus))})
    idx.set_model_id(MODEL_MARKER)
    return idx


@pytest.fixture()
def rag_index(tmp_path):
    from .corpus import CORPUS
    return build_index(tmp_path, CORPUS)


@pytest.fixture()
def real_models():
    """Real ONNX models — opt-in; skips otherwise. Home = seeded cache or
    the user's prepared dir."""
    if not os.environ.get("RAG_REAL_MODELS"):
        pytest.skip("RAG_REAL_MODELS=1 + a prepared model dir required")
    home = Path(os.environ.get("CK_MODEL_HOME",
                              str(Path.home() / ".confluence-knowledge")))
    from confluence_knowledge import retrieval
    return (retrieval.LocalEmbedder(home),
            retrieval.LocalReranker(home),
            retrieval.LocalSparseEmbedder(home))
