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
