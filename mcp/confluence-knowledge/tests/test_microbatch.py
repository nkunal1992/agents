"""Micro-batched inference: bounded padded tokens per run, input order
preserved in the output. Fakes stand in for the tokenizer and ORT session
so no model files are needed."""

import numpy as np

from confluence_knowledge.retrieval import (EMBED_SPECS, LocalEmbedder,
                                            LocalReranker)


class _FakeEnc:
    def __init__(self, n):
        self.ids = [1] * n
        self.attention_mask = [1] * n


class _FakeTok:
    """A text encodes to len(text) tokens; a [q, doc] pair to the sum
    of its element lengths (like a real tokenized pair)."""

    def encode_batch(self, texts, add_special_tokens=True):
        out = []
        for t in texts:
            n = (sum(len(x) for x in t) if isinstance(t, list)
                 else len(t))
            out.append(_FakeEnc(n))
        return out


class _FakeSess:
    def __init__(self):
        self.calls = []  # (B, L) of every run

    def run(self, outputs, feed):
        ids = feed["input_ids"]
        self.calls.append(ids.shape)
        b = ids.shape[0]
        if outputs[0] == "sentence_embedding":
            # row j = one-hot e_j — batch position recoverable in output
            return [np.eye(8, dtype=np.float32)[np.arange(b)]]
        return [np.arange(b, dtype=np.float32).reshape(b, 1)]


def _embedder(sess, tok, spec=None):
    from confluence_knowledge.retrieval import EMBED_SPECS
    emb = object.__new__(LocalEmbedder)
    emb._sess, emb._tok = sess, tok
    emb._spec = spec or EMBED_SPECS["arctic-xs"]
    return emb


def _reranker(sess, tok):
    rr = object.__new__(LocalReranker)
    rr._sess, rr._tok = sess, tok
    return rr


def test_embed_respects_batch_token_budget(monkeypatch):
    # budget bounds B×L² — the 600-token item can't share (2×600² ≫ 100k)
    monkeypatch.setenv("KNOWLEDGE_BATCH_TOKENS", "100000")
    monkeypatch.setenv("KNOWLEDGE_MAX_BATCH", "4")
    sess, tok = _FakeSess(), _FakeTok()
    emb = _embedder(sess, tok, EMBED_SPECS["nomic"])
    texts = ["x" * 600, "a" * 20, "b" * 20, "c" * 20]
    out = emb.embed(texts)
    assert out.shape == (4, 8)
    # nomic task prefixes ride inside the input — "search_document: "
    # is 17 chars, so the fake's chars-as-tokens lengths all grow by 17
    pad = len(EMBED_SPECS["nomic"]["doc_prefix"])
    for b, l in sess.calls:
        assert b * l * l <= 100_000 or b == 1  # the outlier alone
    assert max(l for b, l in sess.calls) == 600 + pad
    assert len(sess.calls) == 2               # outlier + packed rest


def test_embed_preserves_input_order(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_BATCH_TOKENS", raising=False)
    monkeypatch.delenv("KNOWLEDGE_MAX_BATCH", raising=False)
    sess, tok = _FakeSess(), _FakeTok()
    emb = _embedder(sess, tok)
    # lengths 1,2,3 → desc order [2,1,0]; fake row j = one-hot e_j, so
    # input i must land at its desc rank: i0→e2, i1→e1, i2→e0
    out = emb.embed(["a", "bb", "ccc"])
    assert np.allclose(out[0], np.eye(8)[2])
    assert np.allclose(out[1], np.eye(8)[1])
    assert np.allclose(out[2], np.eye(8)[0])


def test_embed_empty_input():
    emb = _embedder(_FakeSess(), _FakeTok())
    out = emb.embed([])
    assert out.shape == (0, 1024)


def test_rerank_micro_batches_and_order(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_BATCH_TOKENS", "100000")
    monkeypatch.setenv("KNOWLEDGE_MAX_BATCH", "2")
    sess, tok = _FakeSess(), _FakeTok()
    rr = _reranker(sess, tok)
    docs = ["x" * 600, "aa", "bb", "cc", "dd"]
    out = rr.score("q", docs)
    assert out.shape == (5,)
    assert all(b * l * l <= 100_000 or b == 1
               for b, l in sess.calls)
    # fake logits row j = j → score sigmoid(j) at its batch position;
    # desc batches are [0]|[1,2]|[3,4], order restored to input
    sig = lambda x: 1.0 / (1.0 + np.exp(-x))
    expected = [sig(0.0), sig(0.0), sig(1.0), sig(0.0), sig(1.0)]
    assert np.allclose(out, expected)


def test_rerank_empty():
    rr = _reranker(_FakeSess(), _FakeTok())
    assert rr.score("q", []).shape == (0,)


def test_task_prefixes_split_roles_and_never_collide(monkeypatch):
    """nomic contract: documents carry `search_document: `, queries carry
    `search_query: ` — and the dedup cache must not let the same raw text
    share a vector across roles."""
    seen = []

    class T:
        def encode_batch(self, ts, **k):
            seen.extend(ts)
            return [_FakeEnc(len(t)) for t in ts]

    sess, _ = _FakeSess(), None
    emb = _embedder(sess, T(), EMBED_SPECS["nomic"])
    emb.embed(["alpha"])                  # document role
    emb.embed(["alpha"], query=True)      # query role — must re-embed
    assert len(emb._vec_cache) == 2
    assert seen[0] == "search_document: alpha"
    assert seen[1] == "search_query: alpha"


def test_arctic_prefixes_split_roles(monkeypatch):
    """#249: arctic's contract — queries carry 'Represent this
    sentence…', documents ride bare; dedup still separates roles."""
    seen = []

    class T:
        def encode_batch(self, ts, **k):
            seen.extend(ts)
            return [_FakeEnc(len(t)) for t in ts]

    sess, _ = _FakeSess(), None
    emb = _embedder(sess, T(), EMBED_SPECS["arctic-xs"])
    emb.embed(["alpha"])                  # document role — no prefix
    emb.embed(["alpha"], query=True)      # query role — arctic prefix
    assert len(emb._vec_cache) == 2
    assert seen[0] == "alpha"
    assert seen[1].startswith(
        "Represent this sentence for searching relevant passages:")


def test_embed_masked_mean_all_padded_row(monkeypatch):
    """Pillar-3 chaos: an all-padding row's mean must not ZeroDivision —
    the denominator is clamped at 1e-9."""
    import numpy as np

    class S:
        def run(self, outs, feed):
            # last_hidden_state only — forces the masked-mean path
            return [np.ones((feed["input_ids"].shape[0],
                             feed["input_ids"].shape[1], 4))]

    class T:
        def encode_batch(self, ts, **k):
            return [type("E", (), {"ids": [1], "attention_mask": [0]})()
                    for _ in ts]          # one token, mask=0 → all-pad

    emb = _embedder(S(), T())
    emb._needs_token_types = False
    emb._has_sentence_emb = False
    emb._dim = 4
    v = emb.embed([""])
    assert np.isfinite(v).all()
