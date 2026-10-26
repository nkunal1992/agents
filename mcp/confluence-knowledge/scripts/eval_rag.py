#!/usr/bin/env python3
"""Golden-set RAG benchmark — real ONNX models, opt-in (not per-PR CI).

    CK_MODEL_HOME=<prepared-models-dir> python scripts/eval_rag.py \
        [--golden PATH] [--report PATH] [--merge-verdicts PATH]

Deterministic scores per pair:
  recall_hit    — EVERY required_pages entry appears among evidence pages
                  (any_source tracks the old any-page signal separately)
  facts_covered — every required_facts substring found in evidence text
  honest_empty  — expect_no_answer pairs return zero evidence

Aggregates: recall@8, fact-coverage %, honesty %, p50/p95 latency.

Writes eval_report.json (question, ideal_answer, evidence, page titles,
deterministic scores) for the Copilot Judge persona — see
prompts/rag-judge.prompt.md — then `--merge-verdicts judge_verdicts.json`
folds the LLM verdicts into benchmark_report.md.

Exits non-zero if recall < 0.80 or honesty < 100% — the regression gate.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).parents[1] / "tests" / "rag"))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import os

os.environ.setdefault(
    "CONF_KNOWLEDGE_HOME",
    os.path.expanduser(os.environ.get("CK_MODEL_HOME", "")))

from confluence_knowledge import retrieval
from confluence_knowledge.extract import extract_chunks
from confluence_knowledge.index import Index

MIN_RECALL = 0.80


def _load_corpus():
    from rag.corpus import CORPUS
    return CORPUS


def _build_index(corpus, emb, sp, tmp):
    idx = Index(tmp / "eval.db")
    for i, (title, html, _qa) in enumerate(corpus):
        pid = f"p{i}"
        chunks, _ = extract_chunks(html)
        for c in chunks:
            c.chunk_id = f"{pid}:h:{c.ordinal}"
        texts = [f"{c.heading_path}\n{c.text}" for c in chunks]
        vecs = emb.embed(texts) if texts else None
        sparse = sp.embed(texts) if texts else None
        idx.upsert_page({"page_id": pid, "parent_id": None, "title": title,
                         "url": f"https://x/{pid}", "level": 0,
                         "version": "", "warnings": ""},
                        chunks, vecs=vecs, sparse=sparse)
    idx.publish({f"p{i}" for i in range(len(corpus))})
    idx.set_model_id(retrieval.MODEL_MARKER)
    return idx


def _score(pair, ev):
    """Deterministic pair score — recall requires ALL required sources."""
    if pair.get("expect_no_answer"):
        return {"recall_hit": True, "facts_covered": True,
                "any_source": None, "sources_missing": [],
                "honest_empty": not ev}
    pages = {e.get("page_title", "") + " " + e.get("page_url", "")
             for e in ev}
    texts = " || ".join(e["text"] for e in ev)
    required = pair.get("required_pages", [])
    missing = [rp for rp in required
               if not any(rp in p for p in pages)]
    recall = (not missing) if required else bool(ev)
    facts = all(f in texts for f in pair.get("required_facts", []))
    return {"recall_hit": recall,
            "any_source": (len(missing) < len(required)) if required else None,
            "sources_missing": missing,
            "facts_covered": facts,
            "honest_empty": None}


def main() -> int:
    args = sys.argv[1:]
    golden = Path(args[args.index("--golden") + 1]) if "--golden" in args \
        else Path(__file__).parents[1] / "tests" / "rag" / "golden.json"
    report_p = Path(args[args.index("--report") + 1]) if "--report" in args \
        else Path("eval_report.json")

    # merge-only mode — fold Copilot verdicts into an existing report
    if "--merge-verdicts" in args:
        vpath = Path(args[args.index("--merge-verdicts") + 1])
        rep = json.loads(report_p.read_text(encoding="utf-8"))
        known_q = {p["question"] for p in rep["pairs"]}
        valid_v = {"correct", "partial", "wrong", "no_answer"}
        verdicts = {}
        skipped = []
        for v in json.loads(vpath.read_text(encoding="utf-8")):
            if not isinstance(v, dict) or not v.get("question") \
                    or v.get("verdict") not in valid_v or v["question"] not in known_q:
                skipped.append(v)
            else:
                verdicts[v["question"]] = v
        n = 0
        for pair in rep["pairs"]:
            v = verdicts.get(pair["question"])
            if v:
                pair["judge"] = v
                n += 1
        report_p.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        ok = sum(1 for v in verdicts.values() if v.get("verdict") == "correct")
        print(f"merged {n}/{len(rep['pairs'])} verdicts — "
              f"judge: {ok}/{len(verdicts)} correct")
        if skipped:
            print(f"  warning: {len(skipped)} verdict(s) ignored — "
                  "bad schema or unknown question")
        return 0

    pairs = json.loads(golden.read_text(encoding="utf-8"))["pairs"]
    home = Path(os.path.expanduser(os.environ["CONF_KNOWLEDGE_HOME"]))
    emb = retrieval.LocalEmbedder(home)
    rr = retrieval.LocalReranker(home)
    sp = retrieval.LocalSparseEmbedder(home)
    print(f"models loaded from {home}")

    if "--index" in args:
        # evaluate a live, already-crawled index (e.g. post-e2e bootstrap)
        idx = Index(Path(args[args.index("--index") + 1]))
    else:
        idx = _build_index(_load_corpus(), emb, sp, Path(tempfile.mkdtemp()))
    print(f"index: {len(idx.active_chunks())} chunks — "
          f"{len(pairs)} golden pairs")

    # optional real candidate answers produced by an agent run —
    # {question: answer_text} — lets the judge grade the answer itself,
    # not just the retrieved evidence (R11)
    candidates = {}
    if "--candidate-answers" in args:
        candidates = json.loads(
            Path(args[args.index("--candidate-answers") + 1]).read_text(encoding="utf-8"))

    results, lat = [], []
    for pair in pairs:
        t = time.monotonic()
        ev = retrieval.retrieve(idx, pair["question"], emb, rr, sp)
        lat.append(time.monotonic() - t)
        results.append({
            "question": pair["question"],
            "ideal_answer": pair["ideal_answer"],
            "candidate_answer": candidates.get(pair["question"]),
            "pages": sorted({e.get("page_title", "") for e in ev}),
            "evidence_texts": [e["text"] for e in ev],
            "n_evidence": len(ev),
            **_score(pair, ev),
        })

    q = [r for r in results if r["honest_empty"] is None]
    hn = [r for r in results if r["honest_empty"] is not None]
    lat.sort()
    recall = sum(r["recall_hit"] for r in q) / max(len(q), 1)
    facts = sum(r["facts_covered"] for r in q) / max(len(q), 1)
    # vacuous truth: a set with no unanswerable pairs measures honesty of
    # nothing — report 100% rather than 0% (would fail every gate)
    honesty = (sum(r["honest_empty"] for r in hn) / len(hn)) if hn else 1.0

    report = {"golden_set": golden.name, "pairs": results,
              "aggregate": {"recall@8": recall, "fact_coverage": facts,
                            "honesty": honesty,
                            "p50_ms": int(lat[len(lat) // 2] * 1000),
                            "p95_ms": int(lat[int(len(lat) * .95)] * 1000)}}
    report_p.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\nrecall@8:       {recall:.0%}  ({sum(r['recall_hit'] for r in q)}/{len(q)})")
    print(f"fact coverage:  {facts:.0%}")
    print(f"honesty:        {honesty:.0%}  ({sum(r['honest_empty'] for r in hn)}/{len(hn)} unanswerable)")
    print(f"latency:        p50={report['aggregate']['p50_ms']}ms "
          f"p95={report['aggregate']['p95_ms']}ms")
    for r in results:
        if r["recall_hit"] is False or r["facts_covered"] is False \
                or r["honest_empty"] is False:
            print(f"  miss: {r['question']!r}")

    print(f"\nreport → {report_p}  (judge it via prompts/rag-judge.prompt.md)")
    ok = recall >= MIN_RECALL and facts >= MIN_RECALL and honesty == 1.0
    print("EVAL", "OK" if ok else "BELOW THRESHOLD")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
