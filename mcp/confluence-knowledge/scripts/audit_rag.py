import argparse
import importlib.metadata
import json
import os
import platform
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(SERVICE / "src"), str(SERVICE / "tests" / "rag")]
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import numpy as np
from audit_corpus import build_audit_corpus

from confluence_knowledge import retrieval as r
from confluence_knowledge.extract import Chunk, extract_chunks
from confluence_knowledge.index import Index


def metrics(pair, evidence):
    required = set(pair["required"])
    found = {c["page_id"] for c in evidence}
    source_chunks = [c for c in evidence if c["page_id"] in required]
    relevant = [c for c in source_chunks if any(f in c["text"] for f in pair["facts"])]
    text = "\n".join(c["text"] for c in source_chunks)
    return {
        "source_recall": len(required & found) / len(required) if required else None,
        "all_sources": required <= found if required else None,
        "precision": len(relevant) / len(evidence) if evidence and required else None,
        "reciprocal_rank": next((1 / (i + 1) for i, c in enumerate(evidence)
                                 if c in relevant), 0) if required else None,
        "facts": all(f in text for f in pair["facts"]) if required else None,
        "nonempty_unanswerable": bool(evidence) if not required else None,
    }


def summarize(rows):
    return {key: float(np.mean(vals)) if vals else None
            for key in rows[0]
            for vals in [[row[key] for row in rows if row[key] is not None]]}


def quality(home, out):
    start = time.perf_counter()
    emb, sp, rr = r.LocalEmbedder(home), r.LocalSparseEmbedder(home), r.LocalReranker(home)
    load_s = time.perf_counter() - start
    pages, questions = build_audit_corpus()
    idx = Index(out / "quality.db")
    token_lengths = []
    build = time.perf_counter()
    for p in pages:
        chunks, _ = extract_chunks(p["html"])
        for c in chunks:
            c.chunk_id = f"{p['id']}:{c.ordinal}"
        texts = [f"{c.heading_path}\n{c.text}" for c in chunks]
        emb._tok.no_truncation()
        token_lengths.extend(len(e.ids) for e in emb._tok.encode_batch(texts))
        emb._tok.enable_truncation(r.EMBED_MAX_TOKENS)
        idx.upsert_page({"page_id": p["id"], "title": p["title"],
                         "url": f"https://audit.invalid/{p['id']}"},
                        chunks, emb.embed(texts), sp.embed(texts))
    idx.publish({p["id"] for p in pages})
    idx.set_model_id(r.MODEL_MARKER)
    build_s = time.perf_counter() - build
    chunks = idx.active_chunks()
    by_id = {c["chunk_id"]: c for c in chunks}
    vectors, sparse = idx.vectors_for(list(by_id)), idx.sparse_for(list(by_id))
    results = []
    for pair in questions:
        t = time.perf_counter()
        q = pair["question"]
        dense = [cid for cid, _ in r._cosine(emb.embed([q])[0], vectors)][:30]
        ft = [cid for cid, _ in idx.fts_search(q, active=set(by_id))]
        sr = [cid for cid, _ in r._sparse_rank(sp.embed([q])[0], sparse)][:30]
        fused = {}
        for leg in (dense, ft, sr):
            for rank, cid in enumerate(leg):
                fused[cid] = fused.get(cid, 0) + 1 / (61 + rank)
        ordered = sorted(fused, key=lambda cid: -fused[cid])[:30]
        candidates = [by_id[cid] for cid in ordered[:25]]
        scores = rr.score(q, [c["text"] for c in candidates])
        full = [dict(candidates[i], score=float(scores[i])) for i in np.argsort(-scores)[:8]
                if scores[i] >= r.MIN_RERANK_SCORE]
        stages = {name: [by_id[cid] for cid in ids[:8]]
                  for name, ids in (("dense", dense), ("fts", ft), ("sparse", sr),
                                    ("hybrid", ordered))}
        stages["full"] = full
        actual = r.retrieve(idx, q, emb, rr, sp)
        assert [c["chunk_id"] for c in actual] == [c["chunk_id"] for c in full]
        row = {**pair, "metrics": {name: metrics(pair, ev) for name, ev in stages.items()},
               "candidate_recall_30": metrics(pair, [by_id[c] for c in ordered])["source_recall"],
               "candidate_recall_25": metrics(pair, candidates)["source_recall"],
               "ranks": {"dense": dense, "fts": ft, "sparse": sr, "fused": ordered},
               "evidence": full, "experiment_seconds": time.perf_counter() - t}
        results.append(row)
        print(pair["id"], row["metrics"]["full"], flush=True)
        (out / "quality.partial.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    report = {"model_load_s": load_s, "index_build_s": build_s,
              "pages": len(pages), "chunks": len(chunks),
              "tokens": {"max": max(token_lengths),
                         "over_512": sum(n > 512 for n in token_lengths)},
              "aggregates": {stage: summarize([row["metrics"][stage] for row in results])
                             for stage in ("dense", "fts", "sparse", "hybrid", "full")},
              "categories": {cat: summarize([row["metrics"]["full"] for row in results
                                             if row["category"] == cat])
                             for cat in sorted({q["category"] for q in questions})},
              "pairs": results}
    idx.db.close()
    return report


def performance(home, out, sizes, samples, words):
    import resource

    start = time.perf_counter()
    emb, sp, rr = r.LocalEmbedder(home), r.LocalSparseEmbedder(home), r.LocalReranker(home)
    load = time.perf_counter() - start
    templates = [f"Service team-{i} deploys on Thursday. Owner group OPER-{i}. "
                 f"Timeout {100+i} milliseconds. Rollback requires the incident commander."
                 for i in range(100)]
    padding = ["Operators", "verify", "recovery", "procedures", "before", "approving", "the", "change"]
    templates = [" ".join((text.split() + padding * words)[:words]) for text in templates]
    dense, sparse = emb.embed(templates), sp.embed(templates)
    reports = []
    for size in sizes:
        path = out / f"scale-{size}.db"
        idx = Index(path)
        chunks = [Chunk(templates[i % 100], "", "text", i, f"p:{i}") for i in range(size)]
        idx.upsert_page({"page_id": "p", "title": "Synthetic scale", "url": "https://audit.invalid/p"},
                        chunks, dense[np.arange(size) % 100],
                        [sparse[i % 100] for i in range(size)])
        idx.publish({"p"})
        times = []
        stage_times = {}

        def timed(obj, name, label, timings):
            original = getattr(obj, name)

            def call(*args, **kwargs):
                tick = time.perf_counter()
                result = original(*args, **kwargs)
                timings.setdefault(label, []).append(time.perf_counter() - tick)
                return result
            setattr(obj, name, call)
            return original

        originals = [(obj, name, timed(obj, name, label, stage_times)) for obj, name, label in (
            (idx, "active_chunks", "chunks_sql"), (idx, "vectors_for", "vectors_sql"),
            (idx, "sparse_for", "sparse_sql"), (idx, "fts_search", "fts"),
            (emb, "embed", "query_embedding"), (sp, "embed", "query_sparse"),
            (rr, "score", "reranking"))]
        for i in range(samples + 1):
            tick = time.perf_counter()
            r.retrieve(idx, f"What timeout and owner does service team-{i % 100} have?", emb, rr, sp)
            times.append(time.perf_counter() - tick)
        for obj, name, original in originals:
            setattr(obj, name, original)
        reports.append({"chunks": size, "samples": samples,
                        "first_s": times[0], "p50_s": float(np.median(times[1:])),
                        "p95_s": float(np.percentile(times[1:], 95)),
                        "raw_s": times[1:],
                        "stage_p50_s": {k: float(np.median(v[1:])) for k, v in stage_times.items()},
                        "rss_peak_native": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        "db_bytes": sum(p.stat().st_size for p in out.glob(path.name + "*"))})
        idx.db.close()
        print(reports[-1], flush=True)
    return {"load_s": load, "template_words": words,
            "repeated_template_scale_only": True, "scales": reports}


def rescore(out):
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    with sqlite3.connect(f"file:{out / 'quality.db'}?mode=ro", uri=True) as db:
        by_id = {cid: {"chunk_id": cid, "page_id": pid, "text": text}
                 for cid, pid, text in db.execute("SELECT chunk_id,page_id,text FROM chunks")}
    for row in report["pairs"]:
        row["metrics"] = {
            stage: metrics(row, row["evidence"] if stage == "full" else
                           [by_id[c] for c in row["ranks"][rank_key][:8]])
            for stage, rank_key in (("dense", "dense"), ("fts", "fts"), ("sparse", "sparse"),
                                    ("hybrid", "fused"), ("full", None))}
    report["aggregates"] = {
        stage: summarize([row["metrics"][stage] for row in report["pairs"]])
        for stage in ("dense", "fts", "sparse", "hybrid", "full")}
    report["categories"] = {
        cat: summarize([row["metrics"]["full"] for row in report["pairs"]
                        if row["category"] == cat])
        for cat in sorted({row["category"] for row in report["pairs"]})}
    report["rescoring_note"] = (
        "No retrieval rerun: chunk precision/MRR require a source match and an expected fact; "
        "nonempty unanswerable evidence is not labelled a hallucinated answer.")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("quality", "performance", "rescore"), default="quality")
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000])
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--words", type=int, default=240)
    args = parser.parse_args()
    if args.samples < 1 or args.words < 24 or any(n < 1 or n > 100000 for n in args.sizes):
        parser.error("samples must be positive; words >= 24; sizes in 1..100000")
    def deny_network(event, _args):
        if event in ("socket.connect", "socket.getaddrinfo"):
            raise RuntimeError("audit runner forbids network connections")
    sys.addaudithook(deny_network)
    out = args.output or Path(tempfile.mkdtemp(prefix="rag-audit-"))
    out.mkdir(parents=True, exist_ok=True)
    if list(out.iterdir()) and args.mode != "rescore":
        parser.error("output must be empty to avoid mixing runs")
    env = {"python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
           "packages": {p: importlib.metadata.version(p)
                        for p in ("numpy", "onnxruntime", "fastembed", "tokenizers", "mcp")},
           "source": r.__file__, "home": str(args.home),
           "python_socket_network": "denied"}
    print(json.dumps(env, indent=2), flush=True)
    if args.mode == "quality":
        result = quality(args.home, out)
    elif args.mode == "performance":
        result = performance(args.home, out, args.sizes, args.samples, args.words)
    else:
        result = rescore(out)
    destination = out / ("rescored.json" if args.mode == "rescore" else "report.json")
    destination.write_text(json.dumps({"environment": env, **result}, indent=2), encoding="utf-8")
    print(destination, flush=True)


if __name__ == "__main__":
    main()
