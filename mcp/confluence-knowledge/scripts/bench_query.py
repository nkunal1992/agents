#!/usr/bin/env python3
"""Query-path leg bench (#255): per-leg retrieve() timing + cache-build cost.

    KNOWLEDGE_MODELS_DIR=~/.knowledge/models \
        python scripts/bench_query.py --chunks 1000,10000,50000 --reps 25

Builds synthetic live-snapshot indexes at each scale — shape-real but
deterministic (384-dim vectors, ~80-term sparse payloads, prose-text
chunks so FTS works) — then times:

  cache_build  — _SearchCache build, the first-query-after-publish cost
  embed_query  — dense query embedding (real arctic-xs ONNX)
  dense_leg    — ANN top-k (forced) or resident float16 matmul
  fts_leg      — FTS5 search + active-id filter
  sparse_leg   — sparse query_embed + postings accumulate (BM25)
  fuse+fetch   — RRF + chunks_by_ids for fused candidates
  rerank       — xsmall cross-encoder on the fused set
  retrieve_all — the real retrieve() end to end (p50 over reps)

Both dense paths are measured: the matmul default, and ANN via a
lowered MAX_SEARCHABLE_CHUNKS patch (--ann) so <100k scopes exercise
the vec0 path too.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_WORDS = ["deploy", "release", "approval", "pipeline", "staging", "window", "rollback", "sign-off", "freeze", "change", "advisory", "board", "runbook", "handover", "escalation", "incident", "review", "owner", "audit", "checklist", "verify", "integration", "endpoint", "cutover", "tracker", "notify", "downstream", "breaking", "deadline", "production"]
# wider vocab for the sparse term pool — the BM25 tokenizer needs varied
# input to emit a realistic term-id space (a 30-word corpus pools ~30 ids)
_VOCAB = _WORDS + [f"svc-{i}" for i in range(800)] + \
    [f"err-{i}" for i in range(800)] + \
    [f"cfg-{i}k" for i in range(800)] + \
    [f"item{i}" for i in range(800)]


def _term_pool(sp, n_texts: int = 200, seed: int = 11) -> np.ndarray:
    """Real BM25 term ids — sparse query terms must actually HIT the
    postings index or the sparse leg measures ~0. Run the real sparse
    model over synthetic texts once, pool the emitted ids."""

    rng = np.random.default_rng(seed)
    texts = [" ".join(_VOCAB[t] for t in rng.integers(0, len(_VOCAB), 60))
             for _ in range(n_texts)]
    pool: set[int] = set()
    for d in sp.embed(texts):
        pool.update(d.keys())
    return np.fromiter(pool, dtype=np.int64)


def _build_index(path: Path, n_chunks: int, term_pool, chunks_per_page: int = 8,
                 seed: int = 7) -> None:
    """Insert a live snapshot directly — timing target is query cost,
    not crawl, so rows go straight into the live tables."""
    from confluence_knowledge.index import Index

    rng = np.random.default_rng(seed)
    idx = Index(path)
    n_pages = math.ceil(n_chunks / chunks_per_page)
    with idx.db:
        t_rows = rng.integers(0, len(_WORDS), size=(n_chunks, 60))
        for p in range(n_pages):
            pid = f"page-{p:05d}"
            idx.db.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?,1)",
                (pid, "root", f"Page {p}", f"http://x/{pid}", 1, "1",
                 time.time(), f"hash-{p}", "[]"))
            for c in range(chunks_per_page):
                cid = f"{pid}:h{c:02x}:{c}"
                if int(cid.split(":")[0][-5:]) * chunks_per_page + c \
                        >= n_chunks:
                    break
                text = " ".join(_WORDS[t] for t in t_rows[
                    p * chunks_per_page + c])
                idx.db.execute(
                    "INSERT INTO chunks VALUES (?,?,?,?,?,?,1)",
                    (cid, pid, f"Section {c}", "text", c, text))
                idx.db.execute(
                    "INSERT INTO chunks_fts VALUES (?,?)", (cid, text))
                v = rng.random(384).astype(np.float32)
                v /= np.linalg.norm(v) + 1e-9
                idx.db.execute("INSERT INTO vectors VALUES (?,?)",
                               (cid, v.tobytes()))
                n_terms = int(rng.integers(60, 120))
                tids = rng.choice(term_pool, size=n_terms, replace=False)
                tw = rng.random(n_terms).astype(np.float32)
                idx.db.execute(
                    "INSERT INTO sparse_vectors VALUES (?,?)",
                    (cid, tids.astype(np.int64).tobytes() + tw.tobytes()))
        idx.db.execute("INSERT OR REPLACE INTO meta VALUES "
                       "('snapshot_epoch','1')")
    idx._rebuild_term_stats()
    idx.db.commit()
    idx.close()


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", default="10000",
                    help="comma list of index sizes")
    ap.add_argument("--reps", type=int, default=25)
    ap.add_argument("--ann", action="store_true",
                    help="force the vec0 ANN dense path")
    ap.add_argument("--profile-build", action="store_true",
                    help="cProfile the search-cache build per size")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    from confluence_knowledge import retrieval
    from confluence_knowledge.index import Index
    from confluence_knowledge.retrieval import LocalEmbedder, LocalReranker, LocalSparseEmbedder, retrieve
    from confluence_knowledge.settings import shared_home

    if args.ann:
        retrieval.MAX_SEARCHABLE_CHUNKS = 1  # any scope → ANN leg

    queries = ["deployment rollback runbook", "audit checklist verify",
               "production incident escalation owner",
               "integration endpoint cutoff tracker",
               "freeze window sign-off board"]
    home = shared_home()

    models_t = {}
    t = time.monotonic()
    emb = LocalEmbedder(home)
    models_t["embedder_load_s"] = round(time.monotonic() - t, 2)
    t = time.monotonic()
    try:
        sp = LocalSparseEmbedder(home)
        models_t["sparse_load_s"] = round(time.monotonic() - t, 2)
    except retrieval.SparseUnavailable as e:
        sp = None                       # #302 — no sparse wheel here
        models_t["sparse_load_s"] = f"skipped: {e}"
    t = time.monotonic()
    rr = LocalReranker(home)
    models_t["reranker_load_s"] = round(time.monotonic() - t, 2)

    results = {"models": models_t, "ann": args.ann, "sizes": {}}
    # without a real embedder there are no genuine term ids — a synthetic
    # pool still gives the index realistic sparse-shaped rows so
    # cache-build/fusion costs stay honest
    term_pool = (_term_pool(sp) if sp is not None
                 else np.arange(1, 50_000, dtype=np.int64))
    print(f"term pool: {len(term_pool)} "
          f"{'real' if sp is not None else 'synthetic'} ids",
          file=sys.stderr)
    for size in [int(x) for x in args.chunks.split(",")]:
        work = Path(tempfile.mkdtemp(prefix=f"qbench-{size}-"))
        dbp = work / "idx.db"
        t = time.monotonic()
        _build_index(dbp, size, term_pool)
        build_s = time.monotonic() - t
        idx = Index(dbp)

        t = time.monotonic()
        cache = idx.search_cache()
        cache_build = time.monotonic() - t

        # attribute cache-build cost: rerun the build under cProfile —
        # which stage dominates (dense matrix, postings loop, pages)?
        if args.profile_build:
            import cProfile
            import io as _io
            import pstats
            idx.drop_search_cache()
            pr = cProfile.Profile()
            pr.enable()
            idx._build_search_cache(99)
            pr.disable()
            s = _io.StringIO()
            pstats.Stats(pr, stream=s).sort_stats("cumulative") \
                .print_stats(14)
            print(f"--- cache build profile @{size} ---", file=sys.stderr)
            print(s.getvalue(), file=sys.stderr)

        legs: dict[str, list[float]] = {k: [] for k in (
            "embed_query", "dense_leg", "fts_leg", "sparse_leg",
            "fuse_fetch", "rerank", "retrieve_all")}
        dense_mode = "ann" if retrieval.MAX_SEARCHABLE_CHUNKS < size \
            else "matmul"
        for rep in range(args.reps):
            q = queries[rep % len(queries)]
            t = time.monotonic()
            qvec = emb.embed([q], query=True)[0].astype(np.float32)
            legs["embed_query"].append(time.monotonic() - t)

            t = time.monotonic()
            if dense_mode == "ann":
                dense_rank = [c for c, _s in idx.ann_topk(qvec, 30)]
            else:
                qn = qvec / (np.linalg.norm(qvec) + 1e-9)
                scores = np.empty(len(cache.ids), dtype=np.float32)
                for off in range(0, len(cache.ids), 4096):
                    scores[off:off + 4096] = \
                        cache.dense[off:off + 4096].astype(
                            np.float32) @ qn
                top = np.argpartition(-scores, 29)[:30]
                top = top[np.argsort(-scores[top])]
                dense_rank = [cache.ids[i] for i in top]
            legs["dense_leg"].append(time.monotonic() - t)

            t = time.monotonic()
            fts = idx.fts_search(q, active=cache.ids_set)[:30]
            legs["fts_leg"].append(time.monotonic() - t)

            sparse_rank = []
            if sp is not None:
                t = time.monotonic()
                qs = sp.query_embed([q])[0]
                ssc = np.zeros(len(cache.ids), dtype=np.float32)
                for term, qw in qs.items():
                    ent = cache.sparse_idx.get(term)
                    if ent is not None:
                        rows, wts = ent
                        ssc[rows] += np.float32(
                            qw * retrieval._bm25_idf(
                                cache.df, cache.n_docs, term)) * wts
                nz = np.flatnonzero(ssc)
                if nz.size:
                    top = nz[np.argsort(-ssc[nz])][:30]
                    sparse_rank = [cache.ids[i] for i in top]
                legs["sparse_leg"].append(time.monotonic() - t)

            t = time.monotonic()
            rrf: dict[str, float] = {}
            for leg in (dense_rank, [c for c, _ in fts], sparse_rank):
                for rank, cid in enumerate(leg):
                    rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (60 + rank + 1)
            fused = sorted(rrf, key=lambda c: -rrf[c])[:30]
            cands = idx.chunks_by_ids(fused)
            legs["fuse_fetch"].append(time.monotonic() - t)

            t = time.monotonic()
            rr.score(q, [c["text"] for c in cands[:25]])
            legs["rerank"].append(time.monotonic() - t)

            t = time.monotonic()
            retrieve(idx, q, emb, rr, sp)
            legs["retrieve_all"].append(time.monotonic() - t)

        results["sizes"][size] = {
            "index_build_s": round(build_s, 2),
            "cache_build_s": round(cache_build, 3),
            "dense_mode": dense_mode,
            "legs_p50_ms": {k: (round(_pct(v, 50) * 1000, 1)
                              if v else None)
                            for k, v in legs.items()},
            "legs_p95_ms": {k: (round(_pct(v, 95) * 1000, 1)
                              if v else None)
                            for k, v in legs.items()},
        }
        idx.close()
        if not args.keep:
            import shutil
            shutil.rmtree(work, ignore_errors=True)

    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
