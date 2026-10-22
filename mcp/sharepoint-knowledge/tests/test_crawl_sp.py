"""Crawl: site → pages+drives expansion, secret-file disclosure, folder
depth, atomic publish."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _fake_graph import BLOBS, DRIVE_ID, LIST_ID, SITE_ID, FakeGraph, site_routes

from confluence_knowledge.index import Index
from sharepoint_knowledge.crawl_sp import SPCrawler


def run_all(c: SPCrawler, job_id: str) -> dict:
    r = c.step(job_id)
    while r["state"] == "running":
        r = c.step(job_id)
    return r


@pytest.fixture()
def idx(tmp_path):
    return Index(tmp_path / "i.db")


def fake_download(url: str) -> bytes:
    return BLOBS[url]


def test_site_expansion_and_publish(idx):
    c = SPCrawler(FakeGraph(site_routes()), idx,
                  downloader=fake_download)
    r = run_all(c, c.start(f"site:{SITE_ID}"))
    assert r["state"] == "done" and r["pages_seen"] == 3
    pages = idx.active_pages()
    assert f"page:{SITE_ID}:{LIST_ID}:pg1" in pages \
        and f"page:{SITE_ID}:{LIST_ID}:pg2" in pages \
        and f"file:{DRIVE_ID}:f2" in pages
    # secret-looking file skipped + disclosed
    assert f"file:{DRIVE_ID}:f3" not in pages
    assert any("secret" in w for w in r["warnings"])
    # site pages carry their SharePoint URLs for citations
    assert pages[f"page:{SITE_ID}:{LIST_ID}:pg1"]["url"].startswith("https://")


def test_chunks_extracted(idx):
    c = SPCrawler(FakeGraph(site_routes()), idx, downloader=fake_download)
    run_all(c, c.start(f"site:{SITE_ID}"))
    chunks = idx.active_chunks()
    text = " ".join(c["text"] for c in chunks)
    assert "CAB sign-off" in text and "Thursdays" in text


def test_folder_scoped_root(idx):
    # root = a single drive item (folder) → only that subtree
    routes = site_routes()
    c = SPCrawler(FakeGraph(routes), idx, downloader=fake_download)
    r = run_all(c, c.start(f"item:{DRIVE_ID}:f1"))
    # f1 is a folder → expands → f3 (secret → disclosed, not indexed)
    assert r["pages_seen"] == 0
    assert any("secret" in w for w in r["warnings"])
    # the site-level list/drive expansion never ran
    assert not any(h == f"sites/{SITE_ID}/lists" for h in
                   c.client.hits)


def test_json_canvas_parts(idx):
    routes = site_routes()
    routes[f"sites/{SITE_ID}/lists/{LIST_ID}/items/pg2"]["fields"][
        "CanvasContent1"] = json.dumps(
            [{"innerHtml": "<p>Deploys on Thursdays.</p>"}])
    c = SPCrawler(FakeGraph(routes), idx, downloader=fake_download)
    run_all(c, c.start(f"site:{SITE_ID}"))
    text = " ".join(c["text"] for c in idx.active_chunks())
    assert "Thursdays" in text


def test_crawl_error_disclosed(idx):
    routes = site_routes()
    del routes[f"drives/{DRIVE_ID}/items/f2"]
    c = SPCrawler(FakeGraph(routes), idx, downloader=fake_download)
    r = run_all(c, c.start(f"site:{SITE_ID}"))
    assert any("f2" in e for e in r["errors"])


def test_seen_checkpoint_across_restart(idx):
    # a second crawl with an identical site re-publishes everything —
    # dedupe is keyed on page_id, not on the job
    c = SPCrawler(FakeGraph(site_routes()), idx, downloader=fake_download)
    r1 = run_all(c, c.start(f"site:{SITE_ID}"))
    r2 = run_all(c, c.start(f"site:{SITE_ID}"))
    assert r2["pages_seen"] == r1["pages_seen"]


def test_unchanged_items_skip_download_and_embedding(idx):
    """Re-crawl: same lastModified → no download; same hash → no
    extract/embed — rows are copied from live instead."""
    calls = {"dl": 0}

    def counting_dl(url: str) -> bytes:
        calls["dl"] += 1
        return BLOBS[url]

    class Emb:
        def __init__(self):
            self.n = 0

        def embed(self, texts):
            self.n += len(texts)
            import numpy as np
            return np.ones((len(texts), 4), dtype=np.float32)

    emb = Emb()
    c = SPCrawler(FakeGraph(site_routes()), idx, embedder=emb,
                  downloader=counting_dl)
    run_all(c, c.start(f"site:{SITE_ID}"))
    assert calls["dl"] == 1 and emb.n > 0

    calls["dl"] = 0
    emb2 = Emb()
    c2 = SPCrawler(FakeGraph(site_routes()), idx, embedder=emb2,
                   downloader=counting_dl)
    r = run_all(c2, c2.start(f"site:{SITE_ID}"))
    assert r["state"] == "done" and r["pages_seen"] == 3
    assert calls["dl"] == 0          # version match skipped the download
    assert emb2.n == 0               # hash match skipped re-embedding
    assert f"file:{DRIVE_ID}:f2" in idx.active_pages()


def test_download_429_retries(fake_sleep=None):
    """download_url retry: 429 → sleep(Retry-After) → retry, bounded."""
    from sharepoint_knowledge.client_sp import download_file
    calls = [0]
    url = "https://dl.test/file"

    class FakeStream:
        def __init__(self, status, body=b"", headers=None):
            self.status_code = status
            self.headers = headers or {}
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self, n):
            yield self._body

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, u):
            calls[0] += 1
            if calls[0] <= 2:
                return FakeStream(429, headers={"retry-after": "0.01"})
            return FakeStream(200, body=b"payload")

    import sharepoint_knowledge.client_sp as cs
    orig = cs.httpx.Client
    cs.httpx.Client = FakeClient
    try:
        out = download_file(url)
    finally:
        cs.httpx.Client = orig
    assert out == b"payload" and calls[0] == 3
