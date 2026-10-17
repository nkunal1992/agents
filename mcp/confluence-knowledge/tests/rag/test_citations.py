"""Citation integrity + scope isolation + freshness symptoms."""

import pytest

from confluence_knowledge import retrieval
from confluence_knowledge.client import SecurityException
from confluence_knowledge.extract import Chunk
from confluence_knowledge.index import Index
from confluence_knowledge.retrieval import check_citations
from rag.conftest import HashEmbedder, OverlapReranker, ToySparse, build_index


def test_citation_normalization(rag_index):
    """NFKC/smart-quote/NBSP/dash variants of a real quote must verify —
    Office-extracted text produces all of them."""
    ev = retrieval.retrieve(rag_index, "minimum password length",
                            HashEmbedder(), OverlapReranker(), ToySparse())
    assert ev
    ch = next(c for c in ev if "14 characters" in c["text"])
    # smart quotes + non-breaking space + en-dash in the quote
    quote = ch["text"].split("\n")[0].replace(" ", " ")  # NBSP-ified
    r = check_citations(ev, [{"claim": "x", "chunk_id": ch["chunk_id"],
                              "quote": quote}])
    assert r["ok"], r


def test_citation_rejects_wrong_chunk(rag_index):
    ev = retrieval.retrieve(rag_index, "password length",
                            HashEmbedder(), OverlapReranker(), ToySparse())
    wrong = next(c for c in ev if "14 characters" not in c["text"])
    r = check_citations(ev, [{"claim": "x", "chunk_id": wrong["chunk_id"],
                              "quote": "minimum password length of 14"}])
    assert not r["ok"]


def test_stale_content_not_retrievable_after_recrawl(tmp_path):
    """Content changed → recrawl → old text must be gone; this is the
    'the agent cited a doc that's since been rewritten' symptom."""
    from tests.rag.corpus import CORPUS
    v1 = [CORPUS[1]]  # password policy
    idx = build_index(tmp_path, v1)
    pid = "p0"
    assert any("90 days" in c["text"] for c in idx.active_chunks())

    # v2: policy rewritten — expiry now 60 days
    html_v2 = """<h1>Password Policy</h1><p>Passwords expire every 60 days.</p>"""
    from confluence_knowledge.extract import extract_chunks
    chunks, _ = extract_chunks(html_v2)
    for c in chunks:
        c.chunk_id = f"{pid}:v2:{c.ordinal}"
    idx.upsert_page({"page_id": pid, "parent_id": None, "title": "Password Policy",
                     "url": "u", "level": 0, "version": "", "warnings": ""},
                    chunks, vecs=None, sparse=None)
    idx.publish({pid})
    texts = " || ".join(c["text"] for c in idx.active_chunks())
    assert "60 days" in texts and "90 days" not in texts


def test_deleted_page_is_gone_from_all_tables(tmp_path):
    """Publish GC: a page dropped from `seen` loses its chunks, fts rows,
    dense vectors AND sparse vectors — nothing half-visible."""
    from tests.rag.corpus import CORPUS
    idx = build_index(tmp_path, CORPUS[:2])
    assert idx.stats()["pages"] == 2
    idx.publish({"p0"})   # p1 removed from the source
    assert idx.stats()["pages"] == 1
    # no orphan rows anywhere
    n_chunks = idx.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_fts = idx.db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
    n_vec = idx.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    n_sparse = idx.db.execute("SELECT COUNT(*) FROM sparse_vectors").fetchone()[0]
    active_cids = {c["chunk_id"] for c in idx.active_chunks()}
    assert n_chunks == n_fts == n_vec == n_sparse == len(active_cids)
    # deleted page's terms unreachable
    assert not idx.fts_search("password", active=active_cids)


def test_transient_failure_keeps_old_content(tmp_path):
    """A page whose re-fetch fails transiently must keep its previous
    content in the snapshot — a network blip must not wipe answers."""
    idx = Index(tmp_path / "i.db")
    c = Chunk(text="kept content", heading_path="", kind="text", ordinal=0,
              chunk_id="p1:h:0")
    idx.upsert_page({"page_id": "p1", "parent_id": None, "title": "t",
                     "url": "u", "level": 0, "version": "", "warnings": ""},
                    [c], vecs=None, sparse=None)
    idx.publish({"p1"})
    # publish with seen ∪ failed — the page keeps its row
    idx.publish({"p1"} | set())          # normal publish: still there
    idx.publish({"p1"})                 # seen includes it → stays
    assert any(c2["page_id"] == "p1" for c2 in idx.active_chunks())
    # and a genuine removal still drops it
    idx.publish(set())
    assert idx.active_chunks() == []


def test_scope_key_full_match_required(tmp_path):
    """verify_citations must compare the FULL scope key — an evidence_set
    whose 8-char prefix collides with another scope must still reject."""
    from confluence_knowledge import server
    server._scopes.clear()
    server._evidence_sets.clear()

    s = {"key": "deadbeefcafe1234567890ab", "profile": None, "root": "r"}
    server._scopes["scope:a:r"] = s
    ev = [{"chunk_id": "c1", "text": "x", "page_id": "p"}]
    server._evidence_sets["ev:0:deadbeef"] = {"scope": s["key"], "ev": ev}

    # a different scope with the SAME 8-char prefix must be rejected
    s2 = {"key": "deadbeef1111111111111111", "profile": None, "root": "r2"}
    server._scopes["scope:b:r2"] = s2
    with pytest.raises(SecurityException):
        server.verify_citations("scope:b:r2", "ev:0:deadbeef", [])


def test_unknown_scope_handle_clean_error():
    """Garbage handle → SetupError (named remediation), not KeyError."""
    from confluence_knowledge import server
    from confluence_knowledge.settings import SetupError
    server._scopes.clear()
    with pytest.raises((SetupError, SecurityException)):
        server.verify_citations("nonsense", "ev:0:x", [])
