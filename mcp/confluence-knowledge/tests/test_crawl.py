"""Crawl: BFS over a mock DC client, resumable steps, snapshot publish."""

import httpx

from confluence_knowledge.client import RULES_DC, ConfluenceClient
from confluence_knowledge.crawl import Crawler
from confluence_knowledge.index import Index

# tiny fixture tree: root 1 → [2 → [3]]
PAGES = {
    "1": {"id": "1", "title": "Root",
          "body": {"storage": {"value": "<p>root body</p>"}},
          "version": {"number": 1}, "ancestors": [],
          "_links": {"webui": "/pages/1", "base": "https://dc"}},
    "2": {"id": "2", "title": "Child",
          "body": {"storage": {"value": "<p>child body</p>"}},
          "version": {"number": 1}, "ancestors": [{"id": "1"}],
          "_links": {"webui": "/pages/2", "base": "https://dc"}},
    "3": {"id": "3", "title": "Grandchild",
          "body": {"storage": {"value": "<p>deep body</p>"}},
          "version": {"number": 1}, "ancestors": [{"id": "1"}, {"id": "2"}],
          "_links": {"webui": "/pages/3", "base": "https://dc"}},
}
CHILDREN = {"1": [{"id": "2"}], "2": [{"id": "3"}], "3": []}


def _dc_client():
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/child/page" in path:
            pid = path.split("/content/")[1].split("/")[0]
            return httpx.Response(200, json={
                "results": CHILDREN.get(pid, []), "size": len(CHILDREN.get(pid, [])),
                "limit": 200})
        pid = path.split("/content/")[1].rstrip("/")
        if pid in PAGES:
            return httpx.Response(200, json=PAGES[pid])
        return httpx.Response(404, json={})
    return ConfluenceClient("https://dc", {}, RULES_DC,
                            session=httpx.Client(
                                transport=httpx.MockTransport(handler)))


def test_crawl_indexes_root_to_grandchild(tmp_path):
    idx = Index(tmp_path / "s.db")
    c = Crawler(_dc_client(), idx, "datacenter")
    job = c.start("1")
    r = c.step(job, batch=10)
    assert r["state"] == "done" and r["pages_seen"] == 3
    pages = idx.active_pages()
    assert set(pages) == {"1", "2", "3"}
    assert pages["3"]["level"] == 2


def test_crawl_resumes_across_steps(tmp_path):
    idx = Index(tmp_path / "s.db")
    c = Crawler(_dc_client(), idx, "datacenter")
    job = c.start("1")
    r1 = c.step(job, batch=1)              # processes root only
    assert r1["state"] == "running" and r1["remaining"] == 1
    # fresh crawler instance (simulates interruption) — queue persisted
    c2 = Crawler(_dc_client(), idx, "datacenter")
    r2 = c2.step(job, batch=10)
    assert r2["state"] == "done" and idx.stats()["pages"] == 3


class _FakeEmbedder:
    def __init__(self):
        self.inputs = []

    def embed(self, texts, query: bool = False):
        import numpy as np
        self.inputs.extend(texts)
        return np.ones((len(texts), 4), dtype=np.float32)


def test_crawl_with_embedder_stores_vectors(tmp_path):
    """Regression #70: the embedding path used a non-existent Chunk.heading."""
    idx = Index(tmp_path / "s.db")
    emb = _FakeEmbedder()
    c = Crawler(_dc_client(), idx, "datacenter", embedder=emb)
    job = c.start("1")
    assert c.step(job, batch=10)["state"] == "done"
    chunk_ids = [ch["chunk_id"] for ch in idx.active_chunks()]
    assert chunk_ids and len(idx.vectors_for(chunk_ids)) == len(chunk_ids)
    assert any("deep body" in t for t in emb.inputs)


def test_step_stops_at_time_budget_and_resumes(tmp_path, monkeypatch):
    """Each step must return before the IDE's request timeout; completed
    pages are checkpointed so the next step continues, not restarts."""
    import confluence_knowledge.crawl as crawl_mod
    clock = iter(range(0, 1000, 10))  # every monotonic() call advances 10 s
    monkeypatch.setattr(crawl_mod.time, "monotonic", lambda: next(clock))
    idx = Index(tmp_path / "s.db")
    c = Crawler(_dc_client(), idx, "datacenter")
    job = c.start("1")
    r1 = c.step(job, batch=50, budget_s=5)   # deadline passes after the first page
    assert r1["state"] == "running" and r1["pages_seen"] == 1
    r2 = Crawler(_dc_client(), idx, "datacenter").step(job, batch=50)
    assert r2["state"] == "done" and r2["pages_seen"] == 3


def test_new_crawl_supersedes_abandoned_job(tmp_path):
    idx = Index(tmp_path / "s.db")
    c = Crawler(_dc_client(), idx, "datacenter")
    stale = c.start("1")                      # never stepped, e.g. a failed attempt
    fresh = c.start("1")
    c.step(fresh, batch=10)
    assert idx.job(stale)["state"] == "abandoned"
    assert idx.latest_job() == {"job_id": fresh, "state": "done", "errors": []}


def test_relative_cloud_links_become_absolute():
    from confluence_knowledge.crawl import _abs_url
    assert _abs_url("/spaces/X/pages/1", "https://a.atlassian.net/wiki") == \
        "https://a.atlassian.net/wiki/spaces/X/pages/1"
    assert _abs_url("https://x/y", "https://ignored") == "https://x/y"
    c = Crawler(None, None, "cloud", site_url="https://a.atlassian.net")
    assert c.site_url == "https://a.atlassian.net/wiki"


def test_out_of_subtree_page_skipped(tmp_path):
    # page 4 claims ancestry not containing root
    PAGES["4"] = dict(PAGES["3"], id="4", title="Outsider",
                      ancestors=[{"id": "99"}])
    CHILDREN["3"] = [{"id": "4"}]
    idx = Index(tmp_path / "s.db")
    c = Crawler(_dc_client(), idx, "datacenter")
    job = c.start("1")
    c.step(job, batch=20)
    assert "4" not in idx.active_pages()
    del CHILDREN["3"], PAGES["4"]
