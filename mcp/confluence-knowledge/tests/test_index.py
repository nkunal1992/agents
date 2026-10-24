"""Index persistence, snapshot publication, FTS, scope isolation."""

import time
import types

import numpy as np

from confluence_knowledge.extract import Chunk
from confluence_knowledge.index import Index


def _page(pid, title):
    return {"page_id": pid, "parent_id": None, "title": title,
            "url": f"/p/{pid}", "level": 0, "version": "1",
            "content_hash": "h", "warnings": []}


def test_upsert_publish_search(tmp_path):
    idx = Index(tmp_path / "s.db")
    chunks = [Chunk("the deploy runbook lives in Jenkins", "", "text", 0, "c1")]
    idx.upsert_page(_page("1", "Deploy"), chunks, np.ones((1, 4), np.float32))
    assert idx.stats()["pages"] == 0          # not yet published
    idx.publish({"1"})
    assert idx.stats()["pages"] == 1
    hits = idx.fts_search("runbook")
    assert hits and hits[0][0] == "c1"
    v = idx.vectors_for(["c1"])
    assert v["c1"].shape == (4,)


def test_snapshot_replaces_removed_pages(tmp_path):
    idx = Index(tmp_path / "s.db")
    idx.upsert_page(_page("1", "A"), [Chunk("a", "", "text", 0, "c1")], None)
    idx.upsert_page(_page("2", "B"), [Chunk("b", "", "text", 0, "c2")], None)
    idx.publish({"1", "2"})
    # second crawl sees only page 1
    idx.upsert_page(_page("1", "A"), [Chunk("a2", "", "text", 0, "c3")], None)
    idx.publish({"1"})
    pages = idx.active_pages()
    assert set(pages) == {"1"}                # page 2 removed from snapshot


def test_scopes_are_separate_dbs(tmp_path):
    a = Index(tmp_path / "scopeA.db")
    b = Index(tmp_path / "scopeB.db")
    a.upsert_page(_page("1", "A"), [Chunk("x", "", "text", 0, "c1")], None)
    a.publish({"1"})
    assert a.stats()["pages"] == 1 and b.stats()["pages"] == 0


def test_job_resume_state(tmp_path):
    idx = Index(tmp_path / "s.db")
    idx.create_job("j1", "root1")
    idx.save_job("j1", "running", ["page:2"], ["page 9: 403"])
    job = idx.job("j1")
    assert job["state"] == "running" and job["queue"] == ["page:2"]
    assert "403" in job["errors"][0]


def test_reads_never_wait_on_write_lock(tmp_path):
    """#188: WAL readers must not queue behind a held writer lock —
    the mid-crawl/publish stalls behind _build_search_cache showed up
    as >2 s status calls at 10k scale."""
    idx = Index(tmp_path / "s.db")
    idx.create_job("j", "1")
    idx.save_job("j", "running", ["a"], [])
    # writer holds the lock mid-transaction — all pure reads must still
    # answer fast via the read-only connection
    with idx.lock:
        t0 = time.monotonic()
        idx.job_progress("j")
        idx.meta_get("embed_model")
        idx.stats()
        idx.active_pages()
        idx.latest_job()
        idx.snapshot_epoch()
        idx.term_stats()
        idx.page_chunks("nope")
        idx.live_page_hash("nope")
        idx.fts_search("x")
        held = time.monotonic() - t0
    assert held < 2.0


def test_search_cache_builds_off_writer_lock(tmp_path, monkeypatch):
    """#188: a seconds-scale cache build must not hold self.lock."""
    idx = Index(tmp_path / "s.db")
    idx.upsert_page({"page_id": "p", "title": "t", "url": "u"},
                    [types.SimpleNamespace(chunk_id="p:1", heading_path="",
                                           kind="p", ordinal=0, text="hi")],
                    None)
    idx.publish({"p"})
    held_during_build = []

    real_execute = idx.db.execute

    def probe_lock(*a, **k):
        held = idx.lock.acquire(blocking=False)
        held_during_build.append(not held)
        if held:
            idx.lock.release()
        return real_execute(*a, **k)

    # probe inside the build via vectors_for's read-path call site
    orig_vf = Index.vectors_for
    def spied(self, ids):
        held = self.lock.acquire(blocking=False)
        held_during_build.append(not held)
        if held:
            self.lock.release()
        return orig_vf(self, ids)
    monkeypatch.setattr(Index, "vectors_for", spied)

    idx.search_cache()
    assert held_during_build and not any(held_during_build)


def test_running_live_job_dedup(tmp_path):
    """start_crawl on a scope with a live-leased running job returns that
    job — a second Start crawl must not abandon in-flight work (field:
    agent issued Start crawl twice → job 1 abandoned mid-flight)."""
    from confluence_knowledge.index import Index
    idx = Index(tmp_path / "s.db")
    idx.create_job("j1", "page:1")
    idx.save_job("j1", "running", ["page:1"], [])
    assert idx.running_live_job() is None     # no lease yet — not 'live'
    idx.acquire_lease("j1", "owner:x", pid=123)
    assert idx.running_live_job() == "j1"
    # stale lease → orphaned, must NOT be returned
    idx.db.execute("UPDATE job_lease SET heartbeat=0 WHERE job_id='j1'")
    idx.db.commit()
    assert idx.running_live_job() is None


def test_ann_backfill_and_topk(tmp_path):
    """#251: vec_chunks mirrors vectors — _ensure_ann backfills in place,
    ann_topk returns cosine-sorted neighbours."""
    idx = Index(tmp_path / "s.db")
    vecs = np.zeros((3, 4), np.float32)
    vecs[0] = [1, 0, 0, 0]
    vecs[1] = [0.9, 0.1, 0, 0]
    vecs[2] = [0, 0, 1, 0]
    for i, cid in enumerate(["c1", "c2", "c3"]):
        idx.upsert_page(_page(f"p{i}", f"P{i}"),
                        [Chunk(f"text {i}", "", "text", 0, cid)],
                        vecs[i:i + 1])
    idx.publish({"p0", "p1", "p2"})
    assert idx.ann_available()
    hits = idx.ann_topk(np.array([1, 0, 0, 0], np.float32), 2)
    assert [h[0] for h in hits] == ["c1", "c2"]
    assert hits[0][1] > hits[1][1]


def test_ann_publish_delta(tmp_path):
    """#251: a later publish mirrors deletes + new rows into vec_chunks —
    no stale neighbours, no missing ones."""
    idx = Index(tmp_path / "s.db")
    idx.upsert_page(_page("1", "A"), [Chunk("a", "", "text", 0, "c1")],
                    np.array([[1, 0, 0, 0]], np.float32))
    idx.upsert_page(_page("2", "B"), [Chunk("b", "", "text", 0, "c2")],
                    np.array([[0, 1, 0, 0]], np.float32))
    idx.publish({"1", "2"})
    assert idx.ann_available()
    # second snapshot: page 2 gone, page 1 has a new vector
    idx.upsert_page(_page("1", "A"), [Chunk("a2", "", "text", 0, "c3")],
                    np.array([[0.99, 0.01, 0, 0]], np.float32))
    idx.publish({"1"})
    hits = idx.ann_topk(np.array([1, 0, 0, 0], np.float32), 5)
    assert {h[0] for h in hits} == {"c3"}        # c1/c2 both evicted


def test_ann_retrieve_over_cap(monkeypatch, tmp_path):
    """#251: >cap whole-scope search answers via ANN instead of
    refusing — the user's real 306k-chunk scope."""
    from confluence_knowledge import retrieval as rt
    monkeypatch.setattr(rt, "MAX_SEARCHABLE_CHUNKS", 3)
    idx = Index(tmp_path / "s.db")
    vecs = np.eye(4, dtype=np.float32)
    for i, cid in enumerate(["c1", "c2", "c3", "c4", "c5"]):
        idx.upsert_page(_page(f"p{i}", f"Topic {i}"),
                        [Chunk(f"text about topic {i}", "", "text", 0, cid)],
                        vecs[i:i + 1])
    idx.publish({f"p{i}" for i in range(5)})

    class E:                        # embeds the query as basis vector e0
        def embed(self, ts, query=False):
            return np.array([[1, 0, 0, 0]] * len(ts), np.float32)

    ev = rt.retrieve(idx, "topic query", E())
    assert ev                              # refused before #251
    assert ev[0]["chunk_id"] == "c1"


def test_ann_unavailable_still_raises(monkeypatch, tmp_path):
    """#251: no extension → the old behaviour: refuse over-cap scopes
    with the connect-narrower message."""
    from confluence_knowledge import retrieval as rt
    monkeypatch.setattr(rt, "MAX_SEARCHABLE_CHUNKS", 3)
    monkeypatch.setattr(Index, "_ensure_ann", lambda self: False)
    idx = Index(tmp_path / "s.db")
    for i in range(5):
        idx.upsert_page(_page(f"p{i}", f"T{i}"),
                        [Chunk(f"t{i}", "", "text", 0, f"c{i}")],
                        np.ones((1, 4), np.float32))
    idx.publish({f"p{i}" for i in range(5)})

    class E:
        def embed(self, ts, query=False):
            return np.ones((len(ts), 4), np.float32)

    import pytest

    from confluence_knowledge.settings import SetupError
    with pytest.raises(SetupError, match="narrower scope"):
        rt.retrieve(idx, "q", E())
