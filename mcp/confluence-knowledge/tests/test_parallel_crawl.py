"""Parallel fetch pipeline: bounded concurrency, unchanged-row reuse, resume.

The handler sleeps per request so a sequential crawl would take
pages × delay; concurrency is measured deterministically by tracking the
max simultaneous in-flight requests inside the mock transport.
"""

import threading
import time

import httpx
import numpy as np
import pytest

from confluence_knowledge.client import RULES_DC, ConfluenceClient
from confluence_knowledge.crawl import Crawler
from confluence_knowledge.index import Index

N_KIDS = 24


def _fan_client(delay=0.05, peak=None, calls=None):
    """root 1 → N_KIDS children; every request sleeps `delay`."""
    pages = {"1": {"id": "1", "title": "Root",
                   "body": {"storage": {"value": "<p>root body</p>"}},
                   "version": {"number": 1}, "ancestors": [],
                   "_links": {"webui": "/p/1", "base": "https://dc"}}}
    children = {"1": [{"id": str(i)} for i in range(2, 2 + N_KIDS)]}
    for i in range(2, 2 + N_KIDS):
        pid = str(i)
        pages[pid] = {"id": pid, "title": f"Page {pid}",
                      "body": {"storage": {"value":
                                           f"<p>content of {pid}</p>"}},
                      "version": {"number": 1},
                      "ancestors": [{"id": "1"}],
                      "_links": {"webui": f"/p/{pid}", "base": "https://dc"}}
    cur = [0]
    lock = threading.Lock()

    def handler(req: httpx.Request) -> httpx.Response:
        with lock:
            cur[0] += 1
            if peak is not None and cur[0] > peak[0]:
                peak[0] = cur[0]
        try:
            time.sleep(delay)
            if calls is not None:
                calls.append(req.url.path)
            path = req.url.path
            if "/child/page" in path:
                pid = path.split("/content/")[1].split("/")[0]
                kids = children.get(pid, [])
                return httpx.Response(200, json={
                    "results": kids, "size": len(kids), "limit": 200})
            pid = path.split("/content/")[1].rstrip("/")
            if pid in pages:
                return httpx.Response(200, json=pages[pid])
            return httpx.Response(404, json={})
        finally:
            with lock:
                cur[0] -= 1

    return ConfluenceClient("https://dc", {}, RULES_DC,
                            session=httpx.Client(
                                transport=httpx.MockTransport(handler)))


class _CountEmbedder:
    def __init__(self):
        self.n = 0
        self.texts = []

    def embed(self, texts, query: bool = False):
        self.n += len(texts)
        self.texts.extend(texts)
        return np.ones((len(texts), 4), dtype=np.float32)


def test_fetches_run_in_parallel(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "8")
    peak = [0]
    idx = Index(tmp_path / "s.db")
    c = Crawler(_fan_client(delay=0.05, peak=peak), idx, "datacenter")
    job = c.start("1")
    r = c.step(job, batch=None)
    assert r["state"] == "done" and r["pages_seen"] == 1 + N_KIDS
    # sequential fetches would overlap by 1; the pipeline must exceed that
    assert peak[0] > 1


def test_concurrency_bound_is_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "4")
    peak = [0]
    idx = Index(tmp_path / "s.db")
    c = Crawler(_fan_client(delay=0.05, peak=peak), idx, "datacenter")
    job = c.start("1")
    c.step(job, batch=None)
    assert 1 < peak[0] <= 4


def test_parallel_is_much_faster(tmp_path, monkeypatch):
    # measure the sequential baseline on THIS runner — an absolute bound
    # flakes on congested CI (a 2.0s cap failed at 2.875s on Windows)
    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "1")
    idx_seq = Index(tmp_path / "seq.db")
    c = Crawler(_fan_client(delay=0.05), idx_seq, "datacenter")
    job = c.start("1")
    t0 = time.monotonic()
    c.step(job, batch=None)
    sequential = time.monotonic() - t0

    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "8")
    idx = Index(tmp_path / "s.db")
    c = Crawler(_fan_client(delay=0.05), idx, "datacenter")
    job = c.start("1")
    t0 = time.monotonic()
    c.step(job, batch=None)
    elapsed = time.monotonic() - t0
    # parallel must decisively beat the runner's own sequential baseline —
    # the ratio is the signal, not an absolute wall-clock bound
    assert elapsed < sequential * 0.5, \
        f"parallel {elapsed:.2f}s vs sequential {sequential:.2f}s"


def test_unchanged_pages_reuse_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "6")
    idx = Index(tmp_path / "s.db")
    emb = _CountEmbedder()
    c = Crawler(_fan_client(delay=0), idx, "datacenter",
                embedder=emb)
    job = c.start("1")
    c.step(job, batch=None)
    assert emb.n == 1 + N_KIDS
    vecs_before = idx.vectors_for(
        [ch["chunk_id"] for ch in idx.active_chunks()])

    # second crawl over identical content: every page hits the reuse path
    emb2 = _CountEmbedder()
    c2 = Crawler(_fan_client(delay=0), idx, "datacenter", embedder=emb2)
    job2 = c2.start("1")
    r = c2.step(job2, batch=None)
    assert r["state"] == "done" and r["pages_seen"] == 1 + N_KIDS
    assert emb2.n == 0            # nothing re-embedded
    vecs_after = idx.vectors_for(
        [ch["chunk_id"] for ch in idx.active_chunks()])
    # reuse copies the live rows — identical vectors, identical chunk ids
    assert set(vecs_after) == set(vecs_before)


def test_changed_page_reembeds_only_that_page(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "6")
    idx = Index(tmp_path / "s.db")
    c = Crawler(_fan_client(delay=0), idx, "datacenter",
                embedder=_CountEmbedder())
    job = c.start("1")
    c.step(job, batch=None)

    # one page changed → only its chunks get new embeddings
    client = _fan_client(delay=0)
    # mutate page 7 in the fixture by serving different storage
    orig = client._http._transport.handler
    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "6")
    emb = _CountEmbedder()

    def handler(req: httpx.Request) -> httpx.Response:
        resp = orig(req)
        if req.url.path == "/rest/api/content/7":
            d = dict(resp.json())
            d = {**d, "body": {"storage": {"value": "<p>edited body</p>"}}}
            return httpx.Response(200, json=d)
        return resp

    client._http._transport = httpx.MockTransport(handler)
    c2 = Crawler(client, idx, "datacenter", embedder=emb)
    job2 = c2.start("1")
    r = c2.step(job2, batch=None)
    assert r["state"] == "done"
    # only page 7's text went through the embedder
    assert emb.n == 1
    assert any("edited body" in t or "Page 7" in t for t in emb.texts)


def test_bounded_step_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "8")
    idx = Index(tmp_path / "s.db")
    c = Crawler(_fan_client(delay=0), idx, "datacenter")
    job = c.start("1")
    r = c.step(job, batch=3)
    assert r["state"] == "running"
    assert r["pages_seen"] <= 4          # bound respected (±1 in-flight)
    r = c.step(job, batch=None)
    assert r["state"] == "done" and r["pages_seen"] == 1 + N_KIDS


def test_inflight_entries_survive_in_checkpoint(tmp_path, monkeypatch):
    """A bounded step that ends with work in flight must persist those
    entries in the checkpoint queue — resume must cover them."""
    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "8")
    idx = Index(tmp_path / "s.db")
    c = Crawler(_fan_client(delay=0.05), idx, "datacenter")
    job = c.start("1")
    r = c.step(job, batch=2)
    assert r["state"] == "running"
    saved = idx.job(job)
    # every queued + in-flight entry is in the durable queue
    assert len(saved["queue"]) > 0
    r = c.step(job, batch=None)
    assert r["state"] == "done" and r["pages_seen"] == 1 + N_KIDS


def test_transient_transport_error_keeps_content(tmp_path, monkeypatch):
    """A network-level (non-AccessError) failure is transient: the page's
    prior content carries forward instead of dropping out of the index."""
    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "4")
    idx = Index(tmp_path / "s.db")
    c = Crawler(_fan_client(delay=0), idx, "datacenter")
    job = c.start("1")
    c.step(job, batch=None)
    assert len(idx.active_pages()) == 1 + N_KIDS

    client = _fan_client(delay=0)
    orig = client._http._transport.handler

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/rest/api/content/5":
            raise httpx.ConnectError("simulated socket drop")
        return orig(req)

    client._http._transport = httpx.MockTransport(handler)
    c2 = Crawler(client, idx, "datacenter")
    job2 = c2.start("1")
    r = c2.step(job2, batch=None)
    assert r["state"] == "done"
    assert any("5" in e and "ConnectError" in e for e in r["errors"])
    # page 5's rows carried forward — still in the published snapshot
    assert "5" in idx.active_pages()


# ---- 429 / Retry-After ----


def test_429_retried_with_retry_after():
    seen = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        seen[0] += 1
        if seen[0] <= 2:
            return httpx.Response(429, headers={"retry-after": "0.05"})
        return httpx.Response(200, json={"ok": True})

    c = ConfluenceClient("https://dc", {}, RULES_DC,
                         session=httpx.Client(
                             transport=httpx.MockTransport(handler)))
    r = c.get("rest/api/user/current")
    assert r.json() == {"ok": True}
    assert seen[0] == 3


def test_429_exhausts_to_access_error():
    from confluence_knowledge.client import AccessError
    seen = [0]

    def handler(req: httpx.Request) -> httpx.Response:
        seen[0] += 1
        return httpx.Response(429, headers={"retry-after": "0.01"})

    c = ConfluenceClient("https://dc", {}, RULES_DC,
                         session=httpx.Client(
                             transport=httpx.MockTransport(handler)))
    with pytest.raises(AccessError, match="rate limited"):
        c.get("rest/api/user/current")
    assert seen[0] == 4            # 1 try + 3 bounded retries


def test_429_retry_after_header_capped():
    from confluence_knowledge.client import _retry_after_seconds
    assert _retry_after_seconds("3600") == 60.0
    assert _retry_after_seconds(None) == 2.0
    assert _retry_after_seconds("bogus") == 2.0
    assert 0.5 <= _retry_after_seconds("0") <= 60.0
