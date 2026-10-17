#!/usr/bin/env python3
"""Real-model RAG eval — opt-in, NOT run in CI (downloads ~1.5GB models).

    CK_MODEL_HOME=/path/to/prepared/models python scripts/eval_rag.py
    # or with a cached dir from prepare-models:
    CK_MODEL_HOME=$HOME/.confluence-knowledge python scripts/eval_rag.py

Builds the tests/rag corpus against the REAL ONNX stack and reports:
  - recall@8    — gold snippet found in returned evidence text
  - honesty     — no-answer queries return empty evidence
  - p50/p95 ms  — retrieval latency
Exits non-zero if recall or honesty dip below thresholds.
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests" / "rag"))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import os

os.environ.setdefault("CONF_KNOWLEDGE_HOME",
                      os.environ.get("CK_MODEL_HOME", ""))

from rag.corpus import CORPUS, NO_ANSWER

from confluence_knowledge import retrieval
from confluence_knowledge.extract import extract_chunks
from confluence_knowledge.index import Index

MIN_RECALL = 0.80


def main() -> int:
    home = Path(os.environ["CONF_KNOWLEDGE_HOME"])
    emb = retrieval.LocalEmbedder(home)
    rr = retrieval.LocalReranker(home)
    sp = retrieval.LocalSparseEmbedder(home)
    print(f"models loaded: {emb.__class__.__name__} / "
          f"{rr.__class__.__name__} / {sp.__class__.__name__}")

    idx = Index(Path(tempfile.mkdtemp()) / "eval.db")
    for i, (title, html, _qa) in enumerate(CORPUS):
        pid = f"p{i}"
        chunks, _ = extract_chunks(html)
        for c in chunks:
            c.chunk_id = f"{pid}:h:{c.ordinal}"
        texts = [f"{c.heading_path}\n{c.text}" for c in chunks]
        vecs = emb.embed(texts) if texts else None
        sparse = [d for d in sp.embed(texts)] if texts else None
        idx.upsert_page({"page_id": pid, "parent_id": None, "title": title,
                         "url": f"https://x/{pid}", "level": 0,
                         "version": "", "warnings": ""},
                        chunks, vecs=vecs, sparse=sparse)
    idx.publish({f"p{i}" for i in range(len(CORPUS))})
    idx.set_model_id(retrieval.MODEL_MARKER)
    print(f"indexed {len(CORPUS)} pages "
          f"({len(idx.active_chunks())} chunks)")

    lat, hit, miss = [], [], []
    for title, _html, qas in CORPUS:
        for q, gold in qas:
            t = time.monotonic()
            ev = retrieval.retrieve(idx, q, emb, rr, sp)
            lat.append(time.monotonic() - t)
            texts = " || ".join(e["text"] for e in ev)
            (hit if gold in texts else miss).append((title, q, gold))

    honest = sum(1 for q in NO_ANSWER
                 if not retrieval.retrieve(idx, q, emb, rr, sp))
    lat.sort()
    n = len(hit) + len(miss)
    print(f"\nrecall@8:  {len(hit)}/{n} ({len(hit)/n:.0%})")
    print(f"honesty:   {honest}/{len(NO_ANSWER)} no-answer queries empty")
    print(f"latency:   p50={lat[len(lat)//2]*1000:.0f}ms "
          f"p95={lat[int(len(lat)*0.95)]*1000:.0f}ms")
    if miss:
        print("\nmisses:")
        for t_, q_, g_ in miss:
            print(f"  [{t_}] '{q_}' → wanted '{g_}'")

    ok = len(hit) / n >= MIN_RECALL and honest == len(NO_ANSWER)
    print("\nEVAL", "OK" if ok else "BELOW THRESHOLD")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
