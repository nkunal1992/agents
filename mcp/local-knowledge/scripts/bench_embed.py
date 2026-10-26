"""Embed-throughput sweep — measures the REAL LocalEmbedder at each
max_batch on real corpus text. Shows where the curve flattens and what
the self-throttle fix recovers.

    python mcp/local-knowledge/scripts/bench_embed.py [--n 800] [--batches 4,8,16,32,64]

Uses the real ONNX models at ~/.knowledge/models and real corpus text
slices (~400-token chunks, the crawl's actual shape).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def _corpus_texts(n: int) -> list[str]:
    """~400-token real-text slices — matches the crawl's chunk shape."""
    pool_dir = Path("/tmp/lk-realtext")
    if pool_dir.exists():
        pool = "\n\n".join(f.read_text(errors="ignore", encoding="utf-8")
                           for f in sorted(pool_dir.glob("*.txt")))
    else:
        pool = "the quick brown fox jumps over the lazy dog " * 4000
    words = pool.split()
    out = []
    for i in range(n):
        s = (i * 379) % max(1, len(words) - 340)
        out.append(" ".join(words[s:s + 340]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=800)
    ap.add_argument("--batches", default="4,8,16,32,64")
    ap.add_argument("--model", default=None,
                    help="KNOWLEDGE_EMBED_MODEL value — arctic-xs/nomic")
    args = ap.parse_args()
    if args.model:
        os.environ["KNOWLEDGE_EMBED_MODEL"] = args.model

    os.environ.setdefault("LOCAL_KNOWLEDGE_HOME",
                          str(Path(tempfile.mkdtemp()) / "home"))
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    sys.path.insert(0, str(Path(__file__).parent.parent.parent
                            / "confluence-knowledge" / "src"))

    from confluence_knowledge import resources
    from confluence_knowledge.retrieval import LocalEmbedder, active_provider
    from confluence_knowledge.settings import shared_home

    texts = _corpus_texts(args.n)
    print(f"provider: {active_provider()}  |  {len(texts)} texts, "
          f"~{len(texts[0].split())} words each", flush=True)

    results = {}
    for mb in [int(x) for x in args.batches.split(",")]:
        os.environ["KNOWLEDGE_MAX_BATCH"] = str(mb)
        # fresh limits each sweep — env-driven
        resources._cpu_cache[:] = (0.0, 50.0)
        emb = LocalEmbedder(shared_home(), cli="local-knowledge")
        t0 = time.monotonic()
        emb.embed(list(texts))
        dt = time.monotonic() - t0
        rate = len(texts) / dt
        results[mb] = rate
        print(f"  max_batch={mb:3d}  {dt:6.1f}s  "
              f"{rate:6.1f} chunks/s", flush=True)
        del emb
    best = max(results, key=results.get)
    print(f"\nbest: max_batch={best} → {results[best]:.0f} chunks/s")
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
