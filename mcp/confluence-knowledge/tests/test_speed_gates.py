"""Speedup gates for #255 — the optimizations must hold their contracts:

* flush pipelining: fetch submissions continue while an embed flush runs
  (serial code could never submit inside an embed interval — the main
  thread was blocked inside it)
* vectorized postings: argsort-built sparse_idx is element-for-element
  identical to the naive dict-append build it replaced
* publish warm: the search cache is built off the request path after a
  publish — the first query must not pay the build
* parallel model warm: independent loaders finish in ~max, not ~sum;
  battery saver falls back to sequential
"""

import time
from types import SimpleNamespace

import numpy as np
import pytest

import confluence_knowledge.models as models_mod
from confluence_knowledge.crawl import Crawler
from confluence_knowledge.extract import Chunk
from confluence_knowledge.index import Index
from confluence_knowledge.models import ModelPool


@pytest.fixture
def index(tmp_path):
    idx = Index(tmp_path / "g.db")
    yield idx
    idx.db.close()


def _dense_vecs(n, dim=8):
    rng = np.random.default_rng(7)
    return rng.random((n, dim), dtype=np.float32)


class SlowEmbedder:
    """Records embed() call intervals; sleeps to model real ONNX cost."""

    def __init__(self, sleep_s=0.35):
        self.sleep_s = sleep_s
        self.intervals: list[tuple[float, float]] = []
        self.calls = 0

    def embed(self, texts, on_batch=None):
        self.calls += 1
        t0 = time.monotonic()
        if on_batch is not None:
            on_batch(len(texts), len(texts))
        time.sleep(self.sleep_s)
        self.intervals.append((t0, time.monotonic()))
        return _dense_vecs(len(texts))


def test_embed_flush_overlaps_fetch(index):
    """A fetch must START while an embed flush is in flight — the pipeline
    only exists if the main thread keeps the fetch window full during
    embed instead of sitting inside it."""
    fetch_starts: list[float] = []
    pages = {str(i) for i in range(1, 41)}     # 40 pages ≈ 3 flushes
    words = " ".join(["w"] * 50)
    body = " ".join(f"<p>para {i} text {words}</p>"
                    for i in range(40))         # ~9 chunks/page

    class SlowCrawler(Crawler):
        def _page(self, pid):
            fetch_starts.append(time.monotonic())
            time.sleep(0.1)
            return {"storage": body, "title": pid,
                    "url": f"https://x/{pid}", "version": "1"}

        def _children(self, pid, is_folder=False):
            if pid == "1":                    # root fans out to all pages
                return [(p, "page") for p in sorted(pages - {"1"})]
            return []

    emb = SlowEmbedder(sleep_s=0.35)
    crawler = SlowCrawler(None, index, "dc", embedder=emb)
    job = crawler.start("1")
    out = crawler.step(job, batch=None)
    assert out["state"] == "done"
    assert emb.calls >= 2                        # multiple flushes happened
    overlapped = any(s < t < e
                     for t in fetch_starts for s, e in emb.intervals)
    assert overlapped, (
        "no fetch started during any embed — the flush is not pipelined "
        "with the fetch loop")
    assert index.job(job)["state"] == "done"


def _naive_postings(idx):
    """The pre-#255 dict-append build, run on the same rows — ground truth
    for the vectorized path's equivalence."""
    cache = idx.search_cache()
    postings: dict[int, tuple[list[int], list[float]]] = {}
    for cid, blob in idx._read().execute(
            "SELECT s.chunk_id,s.payload FROM sparse_vectors s "
            "JOIN chunks ch ON ch.chunk_id=s.chunk_id WHERE ch.active=1"):
        i = cache.row[cid]
        k = len(blob) // 12
        idx_arr = np.frombuffer(blob[:8 * k], dtype=np.int64)
        val_arr = np.frombuffer(blob[8 * k:], dtype=np.float32)
        for t, w in zip(idx_arr.tolist(), val_arr.tolist()):
            ent = postings.setdefault(t, ([], []))
            ent[0].append(i)
            ent[1].append(w)
    return postings, cache


def test_search_cache_matches_naive_postings(index):
    """The argsort-built sparse index must be element-identical to the
    append-loop build — speed cannot reorder or drop a single posting."""
    for p in range(30):
        chunks = [Chunk(f"text {p} {t}", "", "text", 0, f"c{p}:{t}")
                  for t in range(3)]
        sparse = [{10 + t: 0.5 + t * 0.1, 99 + p % 5: 1.0}
                  for t in range(3)]
        index.upsert_page(
            {"page_id": f"p{p}", "title": f"p{p}", "url": "u",
             "version": "1", "content_hash": f"h{p}"},
            chunks, _dense_vecs(3), sparse)
    index.publish({f"p{p}" for p in range(30)})

    postings, cache = _naive_postings(index)
    assert set(cache.sparse_idx) == set(postings)
    for t, (rows, wts) in cache.sparse_idx.items():
        exp_rows, exp_wts = postings[t]
        np.testing.assert_array_equal(rows, np.asarray(exp_rows,
                                                       dtype=np.int32))
        np.testing.assert_allclose(wts, np.asarray(exp_wts,
                                                   dtype=np.float32))


def test_publish_warms_search_cache(index):
    """After publish the cache must appear without a search ever being
    issued — the query path pays ~0 for the build."""
    index.upsert_page(
        {"page_id": "p", "title": "t", "url": "u", "version": "1"},
        [Chunk("warm me", "", "text", 0, "c0")], _dense_vecs(1),
        [{5: 1.0}])
    index.publish({"p"})
    deadline = time.monotonic() + 5
    while index._cache is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert index._cache is not None, "publish did not warm the cache"
    assert index._cache.epoch == index.snapshot_epoch()


def test_model_warm_loads_kinds_in_parallel(monkeypatch):
    """Three independent 0.2s loaders must finish in ~0.2s, not 0.6s."""
    loaded: list[str] = []

    def fake_loader(name):
        def _load(home, cli):
            time.sleep(0.2)
            loaded.append(name)
            return SimpleNamespace(name=name)
        return _load

    pool = ModelPool(home_fn=lambda: None, cli="x")
    pool._loaders = {k: fake_loader(k) for k in
                     ("embedder", "reranker", "sparse")}
    t0 = time.monotonic()
    pool._warm()
    el = time.monotonic() - t0
    assert sorted(loaded) == ["embedder", "reranker", "sparse"]
    assert el < 0.45, f"warm took {el:.2f}s — loaders serialized"
    assert pool.ready


def test_model_warm_sequential_on_battery(monkeypatch):
    """Battery saver keeps warm sequential — the load spike stays small."""
    order: list[str] = []
    monkeypatch.setattr(models_mod, "limits", lambda: SimpleNamespace(
        battery_saver=True, on_battery=lambda: True))

    def sleepy(name):
        def _load(home, cli):
            time.sleep(0.1)
            order.append(name)
            return SimpleNamespace(name=name)
        return _load

    pool = ModelPool(home_fn=lambda: None, cli="x")
    pool._loaders = {k: sleepy(k) for k in
                     ("embedder", "reranker", "sparse")}
    t0 = time.monotonic()
    pool._warm()
    el = time.monotonic() - t0
    assert order == ["embedder", "reranker", "sparse"]
    # coarse bound — Windows timers run at ~15ms granularity; 0.15 still
    # separates sequential (~0.3s) from a parallel slip (~0.1s)
    assert el >= 0.15, f"warm took {el:.2f}s — ran parallel on battery"
