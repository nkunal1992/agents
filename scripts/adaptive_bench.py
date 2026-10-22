#!/usr/bin/env python3
"""Adaptive-tuning proof — measures embed throughput of a dense crawl
under different KNOWLEDGE_THREADS / KNOWLEDGE_MAX_BATCH settings, then
runs the adaptive default and the battery-pressure simulation.

    python scripts/adaptive_bench.py [--pages 30]

Uses the in-process fake DC (zero network variance) + the dense corpus
bodies from seed_cloud — the same content shape as the real-Cloud
corpus. A fresh index per run keeps the numbers comparable.

Output: a table of embed_ms / pages-per-s per config, then the same
numbers with adaptive defaults, then a pressured run showing the live
scale-down — i.e. the tuning doing its job.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "mcp" / "confluence-knowledge" / "scripts"))
sys.path.insert(0, str(REPO / "scripts"))

from _fake_dc_work import FakeTree, serve


def corpus(pages_n: int) -> FakeTree:
    """Dense tree — medium+long bodies like the real corpus."""
    from seed_cloud import long_dense_body, medium_body
    pages = {"1001": {"title": "Root", "version": 1, "ancestors": [],
                      "body": "<p>root</p>"}}
    children = {"1001": []}
    for i in range(pages_n):
        pid = str(2000 + i)
        dense = (i % 5) in (3, 4)     # ~40% long pages — the mix that
        body = (long_dense_body(f"Spec {i}", 8) if dense
                else medium_body(f"Runbook {i}", 6))
        pages[pid] = {"title": f"Page {i}", "version": 1,
                      "ancestors": ["1001"], "body": body}
        children["1001"].append(pid)
    return FakeTree(pages=pages, children=children, root="1001")


def crawl_once(pages_n: int, env_overrides: dict, label: str) -> dict:
    """One fresh-index crawl at the given limits. Returns the stats the
    daemon records — embed_ms is the number we care about."""
    tmp = Path(tempfile.mkdtemp(prefix="bench-"))
    os.environ["CONF_KNOWLEDGE_WORKDIR"] = str(tmp / "work")
    for k, v in env_overrides.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)

    from confluence_knowledge.crawl import Crawler
    from confluence_knowledge.index import Index
    from confluence_knowledge.resources import limits
    from confluence_knowledge.retrieval import LocalEmbedder, LocalSparseEmbedder

    lim = limits()
    print(f"\n--- {label} (threads={lim.threads} "
          f"max_batch={lim.max_batch}) ---")
    srv, base, _ = serve(corpus(pages_n))
    tree = srv  # keep alive
    client = _client(base)
    idx = Index(tmp / "s.db")
    os.environ.setdefault(
        "KNOWLEDGE_MODELS_DIR",
        str(Path.home() / ".knowledge" / "models"))
    emb = LocalEmbedder(tmp)
    sp = LocalSparseEmbedder(tmp)
    c = Crawler(client, idx, "datacenter", embedder=emb, sparse=sp)
    job = c.start("1001")
    t0 = time.monotonic()
    while True:
        st = c.step(job, batch=None, budget_s=300)
        if st["state"] != "running":
            break
    wall = time.monotonic() - t0
    p = idx.job_progress(job)
    stats = p.get("stats", {})
    stats["wall_s"] = round(wall, 1)
    stats["pages_done"] = p.get("pages_seen")
    stats["label"] = label
    stats["threads"] = lim.threads
    stats["max_batch"] = lim.max_batch
    tree.shutdown()
    return stats


def _client(base):
    from confluence_knowledge.client import RULES_DC, ConfluenceClient
    return ConfluenceClient(base, {"Authorization":
                                   "Bearer stress-token-canary"},
                            RULES_DC)


def row(st: dict) -> str:
    return (f"{st['label']:<22} {st['threads']:>7} {st['max_batch']:>9} "
            f"{st.get('pages_done', 0):>7} {st.get('chunks', 0):>7} "
            f"{st.get('embed_ms', 0) / 1000:>9.1f} "
            f"{st.get('pages_per_s', 0):>9.2f}")


def bench_embed_sweep(texts: list[str], batches: list[int]) -> None:
    """Direct-embedder sweep — chunks/s + peak RSS at each max_batch on
    the real model. The throughput knee tells us where the adaptive
    ceiling should sit (32 is a guess until measured)."""
    sys.path.insert(0, str(REPO / "mcp" / "confluence-knowledge" / "src"))
    import psutil
    from confluence_knowledge.retrieval import LocalEmbedder

    home = Path(tempfile.mkdtemp(prefix="bench-"))
    emb = LocalEmbedder(home)
    proc = psutil.Process()
    print(f"\n{'max_batch':>9} {'chunks/s':>9} {'ms/chunk':>9} "
          f"{'peak_rss_delta':>15}")
    for mb in batches:
        os.environ["KNOWLEDGE_MAX_BATCH"] = str(mb)
        base = proc.memory_info().rss
        emb.embed(texts)                       # warmup
        t0 = time.monotonic()
        emb.embed(texts)
        dt = time.monotonic() - t0
        peak = proc.memory_info().rss - base
        print(f"{mb:>9} {len(texts) / dt:>9.1f} "
              f"{dt / len(texts) * 1000:>9.0f} "
              f"{peak // 1048576:>13} MB")
    os.environ.pop("KNOWLEDGE_MAX_BATCH", None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=30)
    ap.add_argument("--batch-sweep", default=None,
                    help="comma list e.g. 8,16,32,64 — direct embedder "
                         "sweep at each max_batch (no crawl)")
    args = ap.parse_args()

    if args.batch_sweep:
        from seed_cloud import long_dense_body, medium_body
        texts = []
        for i in range(args.pages):
            b = (long_dense_body(f"Spec {i}", 8) if i % 5 in (3, 4)
                 else medium_body(f"Runbook {i}", 6))
            import re
            c = re.sub(r"<[^>]+>", " ", b)
            words = c.split()
            step = max(120, len(words) // 8)
            texts.extend(" ".join(words[j:j + step])
                         for j in range(0, len(words), step))
        print(f"sweep corpus: {len(texts)} chunks")
        bench_embed_sweep(texts,
                          [int(x) for x in args.batch_sweep.split(",")])
        return 0

    try:
        import psutil
        vm = psutil.virtual_memory()
        print(f"machine: {os.cpu_count()} cores, "
              f"{vm.total // 1048576} MB RAM, "
              f"{vm.available // 1048576} MB free, "
              f"cpu {psutil.cpu_percent()}%")
    except ImportError:
        print("machine: psutil missing")

    print(f"\n{'config':<22} {'threads':>7} {'max_batch':>9} "
          f"{'pages':>7} {'chunks':>7} {'embed_s':>9} {'pages/s':>9}")

    results = [
        crawl_once(args.pages, {"KNOWLEDGE_THREADS": "3",
                                "KNOWLEDGE_MAX_BATCH": "4"},
                   "legacy (3t/4b)"),
        crawl_once(args.pages, {"KNOWLEDGE_THREADS": "8",
                                "KNOWLEDGE_MAX_BATCH": "16"},
                   "tuned (8t/16b)"),
        crawl_once(args.pages, {"KNOWLEDGE_THREADS": None,
                                "KNOWLEDGE_MAX_BATCH": None},
                   "adaptive (auto)"),
    ]
    for st in results:
        print(row(st))

    # pressure simulation — battery + a RAM balloon
    try:
        # a RAM balloon shrinks `available` so the tuner sees pressure
        _balloon = bytearray(min(6 * 1024**3, int(vm.available * 0.5)))
        os.environ["KNOWLEDGE_FORCE_BATTERY"] = "1"
        st = crawl_once(6, {"KNOWLEDGE_THREADS": None,
                            "KNOWLEDGE_MAX_BATCH": None},
                        "pressured (battery+RAM)")
        print(row(st))
        assert st["threads"] <= results[2]["threads"], \
            "pressured threads should not exceed adaptive"
        assert st["max_batch"] <= results[2]["max_batch"], \
            "pressured batch should not exceed adaptive"
    finally:
        os.environ.pop("KNOWLEDGE_FORCE_BATTERY", None)
        _balloon = None

    leg, auto = results[0], results[2]
    print(f"\nembed: {leg['embed_ms'] / 1000:.1f}s legacy → "
          f"{auto['embed_ms'] / 1000:.1f}s adaptive "
          f"({leg['embed_ms'] / max(auto['embed_ms'], 1):.1f}x)")
    print(f"pages/s: {leg['pages_per_s']:.2f} → "
          f"{auto['pages_per_s']:.2f}")
    Path("ADAPTIVE_BENCH.json").write_text(json.dumps(results, indent=1))
    print("\nwrote ADAPTIVE_BENCH.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
