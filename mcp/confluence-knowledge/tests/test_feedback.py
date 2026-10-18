"""FeedbackStore: per-scope corrections, threshold matching, integrity."""

import numpy as np

from confluence_knowledge.feedback import FeedbackStore


def _v(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.random(16).astype(np.float32)
    return v / np.linalg.norm(v)


def test_add_and_match(tmp_path):
    st = FeedbackStore(tmp_path, "scope1", "ck-test")
    st.add("password policy?", _v(1), "it is actually 16 chars")
    hits = st.match(_v(1))
    assert len(hits) == 1 and hits[0]["correction"].startswith("it is")
    assert "qvec" not in hits[0] and hits[0]["similarity"] >= 0.85


def test_threshold_rejects_unrelated(tmp_path):
    st = FeedbackStore(tmp_path, "scope1", "ck-test")
    st.add("password policy?", _v(1), "correction")
    # orthogonal vector — sim 0.0 (16-dim random vectors can collide at >0.75)
    q = np.zeros(16, dtype=np.float32)
    q[7] = 1.0
    stored = np.array([np.array(e["qvec"]) for e in st._entries])
    assert float(q @ stored[0]) < 0.75
    assert st.match(q) == []


def test_scopes_isolated(tmp_path):
    a = FeedbackStore(tmp_path, "scopeA", "ck-test")
    b = FeedbackStore(tmp_path, "scopeB", "ck-test")
    a.add("q", _v(1), "for A only")
    assert b.match(_v(1)) == []
    assert a.path.name != b.path.name


def test_corrupt_file_tolerated(tmp_path):
    (tmp_path / "scope1.feedback.json").write_text("{not json!!")
    st = FeedbackStore(tmp_path, "scope1", "ck-test")
    assert st.report()["count"] == 0  # loads empty, doesn't crash


def test_persists_across_instances(tmp_path):
    FeedbackStore(tmp_path, "s", "ck-test").add("q", _v(2), "corr")
    st = FeedbackStore(tmp_path, "s", "ck-test")
    assert st.report()["count"] == 1


def test_report_shape(tmp_path):
    st = FeedbackStore(tmp_path, "s", "ck-test")
    st.add("q1", _v(1), "c1", note="why")
    r = st.report()
    assert r["count"] == 1
    assert r["corrections"][0]["note"] == "why"
    assert "qvec" not in r["corrections"][0]


def test_search_injects_corrections(tmp_path, monkeypatch):
    """submit_feedback → a similar question's search carries it through."""
    from confluence_knowledge import server
    from confluence_knowledge.extract import Chunk
    from confluence_knowledge.index import Index
    server._scopes.clear()
    server._feedback.clear()
    server._evidence_sets.clear()
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()

    s = {"key": "k123", "profile": None, "root": "r"}
    server._scopes["scope:k:r"] = s

    # index with one page so search doesn't early-return "empty"
    idx = Index(tmp_path / ".conf_knowledge_index" / "k123.db")
    c = Chunk(text="old answer text", heading_path="", kind="text",
              ordinal=0, chunk_id="p1:0:0")
    idx.upsert_page({"page_id": "p1", "parent_id": None, "title": "P",
                     "url": "u", "level": 0, "version": "", "warnings": ""},
                    [c], vecs=None, sparse=None)
    idx.publish({"p1"})

    import rag.conftest as rc
    emb = rc.HashEmbedder()
    monkeypatch.setattr(server, "_embedder", lambda: emb)
    monkeypatch.setattr(server, "_reranker", lambda: None)
    monkeypatch.setattr(server, "_sparse", lambda: None)
    monkeypatch.setattr(server, "_models_loaded", lambda _w: True)
    monkeypatch.setattr(server, "_index_for", lambda k: idx)
    monkeypatch.setattr(server, "_feedback_for",
                        lambda k: server._feedback.setdefault(
                            k, server.FeedbackStore(
                                tmp_path / ".conf_knowledge_index",
                                k, "confluence-knowledge")))

    server.submit_feedback("scope:k:r", "what is the password policy",
                           "14 is wrong, it's 16")
    res = server.search("scope:k:r", "what is the password policy")
    assert res["user_corrections"] and \
        res["user_corrections"][0]["correction"] == "14 is wrong, it's 16"
    # unrelated question → no injection
    res2 = server.search("scope:k:r", "zebra quantum xyzzy nonsense")
    assert res2["user_corrections"] == []
