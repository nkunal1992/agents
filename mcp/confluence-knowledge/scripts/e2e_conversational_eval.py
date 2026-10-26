#!/usr/bin/env python3
"""#210 conversational eval — the question-intent matrix through the
REAL tool surface (connect → start_crawl → wait → search →
verify_citations → submit_feedback → feedback_report), real ONNX
models, deterministic grading.

    mcp/confluence-knowledge/.venv/bin/python \
        mcp/confluence-knowledge/scripts/e2e_conversational_eval.py \
        [--report PATH] [--workspace DIR]

Refuses KNOWLEDGE_FAKE_EMBEDDINGS=1 — fake vectors make the grading
vacuous. Needs the model store (KNOWLEDGE_MODELS_DIR /
~/.knowledge/models).

The daemon-http transport of this same journey is covered by
scripts/e2e_real_data.py; this eval isolates the CONVERSATIONAL layer —
question phrasing → evidence → cited answer → honest abstention —
through the identical tool functions the IDE invokes.

Gates (exit non-zero below any):
  recall ≥ 0.80 on gold intents · abstention 100% · adversarial ≥ 0.80
  citation integrity 100% (honest mappings pass, fabricated rejected)
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent                            # mcp/confluence-knowledge
sys.path.insert(0, str(HERE))                      # _fake_dc_work
sys.path.insert(0, str(PKG / "tests" / "rag"))     # eval_corpus, questions

if os.environ.get("KNOWLEDGE_FAKE_EMBEDDINGS") == "1":
    sys.exit("conversational eval refuses KNOWLEDGE_FAKE_EMBEDDINGS=1 "
             "— grading is vacuous on fake vectors")

import _fake_dc_work as dc
import eval_corpus
import questions as qbank

MIN_RECALL = 0.80
MIN_ADVERSARIAL = 0.80
REPORT = Path("CONVERSATIONAL_EVAL_REPORT.md")


def _compose_answer(res: dict, gold: list[str]) -> str:
    """Deterministic answer — the evidence sentences carrying each gold
    fact, joined. What a faithful extractive assistant would say."""
    if not res.get("evidence"):
        return "I don't have documentation for that."
    sents = []
    for e in res["evidence"]:
        for s in e["text"].replace("\n", " ").split(". "):
            if any(g.lower() in s.lower() for g in gold):
                sents.append(s.strip())
    return ". ".join(dict.fromkeys(sents))[:400] or \
        res["evidence"][0]["text"][:200]


def main() -> int:
    # Windows consoles are cp1252 — replace unencodable glyphs (→ ✓ —)
    # in stdout rather than crash at the last line after a full eval
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", default=str(REPORT))
    ap.add_argument("--workspace", default=None)
    args = ap.parse_args()

    ws = Path(args.workspace or
              tempfile.mkdtemp(prefix="conv-eval-")).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    os.environ["CONF_KNOWLEDGE_WORKDIR"] = str(ws / "work")
    os.environ["CONF_KNOWLEDGE_HOME"] = str(ws / "home")
    os.environ["CONF_KNOWLEDGE_DAEMON"] = "0"
    os.environ["CONF_KNOWLEDGE_TOKEN"] = dc.TOKEN

    from confluence_knowledge import server
    from confluence_knowledge.settings import SiteProfile

    pages, children, root = eval_corpus.tree()
    _srv, base, _hits = dc.serve(dc.FakeTree(
        pages=pages, children=children, root=root), dc.Hostility())
    print(f"fake DC serving {len(pages)} pages at {base}", flush=True)

    profile = SiteProfile("datacenter", base, None, "eval")
    SiteProfile.load = staticmethod(lambda home=None: profile)
    # the real client path — credentials flow via CONF_KNOWLEDGE_TOKEN

    c = server.connect(root)
    scope = c["scope_handle"]
    job = server.start_crawl(scope)
    jid = job["job_id"]
    t0 = time.monotonic()
    while True:
        r = server.crawl_step(scope, jid)
        if r["state"] != "running":
            break
        if time.monotonic() - t0 > 600:
            sys.exit("crawl >10min")
        time.sleep(0.5)
    assert r["state"] == "done", f"crawl failed: {r.get('errors')}"
    print(f"crawl done — {r.get('pages_seen')} pages "
          f"({time.monotonic() - t0:.0f}s)", flush=True)

    # models warm on first use — wait out the loader once
    server._models_ready.wait(600)

    rows, per_intent, cite_ok, cite_bad = [], {}, 0, 0
    fabricated_checked = False
    for q in qbank.QUESTIONS:
        t_q = time.monotonic()
        res = server.search(scope, q["q"])
        ms = round((time.monotonic() - t_q) * 1000)
        ev = res.get("evidence") or []
        texts = " || ".join(e["text"] for e in ev).lower()
        gold = q.get("gold", [])

        if q.get("no_answer"):
            verdict = "PASS" if (
                not ev or all(e.get("low_confidence") for e in ev)) \
                else "FAIL"
        elif q["intent"] == "adversarial":
            verdict = "PASS" if all(
                g.lower() in texts for g in gold) else "FAIL"
        elif q["intent"] == "vague":
            # best-effort or honest-empty — either is correct behaviour
            verdict = "PASS"
        elif q.get("any_gold"):
            verdict = "PASS" if any(
                g.lower() in texts for g in gold) else "FAIL"
        else:
            verdict = "PASS" if all(
                g.lower() in texts for g in gold) else "FAIL"

        # citation integrity — honest mapping must verify; one
        # fabricated mapping across the run must be rejected
        cite = "—"
        if ev:
            ok = server.verify_citations(scope, res["evidence_set"],
                [{"claim": q["q"], "chunk_id": ev[0]["chunk_id"],
                  "quote": ev[0]["text"][:60]}])
            if ok.get("ok"):
                cite_ok += 1
                cite = "honest ✓"
            else:
                cite = f"honest ✗ {ok.get('violations')}"
            if not fabricated_checked:
                fabricated_checked = True
                bad = server.verify_citations(scope, res["evidence_set"],
                    [{"claim": "fabricated claim",
                      "chunk_id": "chunk:zzz:0",
                      "quote": "invented sentence"}])
                if bad.get("ok") is False and bad.get("violations"):
                    cite_bad += 1
                    cite += " · fabricated rejected ✓"
                else:
                    cite += " · fabricated ACCEPTED ✗"

        answer = _compose_answer(res, gold)
        server.submit_feedback(scope, q["q"], answer[:120],
                               note=f"intent:{q['intent']}")
        st = per_intent.setdefault(q["intent"],
                                   {"n": 0, "pass": 0, "ms": []})
        st["n"] += 1
        st["pass"] += verdict == "PASS"
        st["ms"].append(ms)
        rows.append((q["q"], q["intent"], verdict, ms, cite,
                     answer[:140]))
        print(f"[{verdict}] {q['intent']:<13} {q['q'][:64]} ({ms}ms)",
              flush=True)

    rep = server.feedback_report(scope)
    n_feedback = rep.get("count", 0)

    # -------- gates --------
    gold_rows = [r for r in rows if r[1] not in
                 ("abstention", "vague")]
    recall = sum(r[2] == "PASS" for r in gold_rows) / max(1, len(gold_rows))
    abst = [r for r in rows if r[1] == "abstention"]
    abstention = sum(r[2] == "PASS" for r in abst) / max(1, len(abst))
    adv = [r for r in rows if r[1] == "adversarial"]
    adversarial = sum(r[2] == "PASS" for r in adv) / max(1, len(adv))
    honest_ok = cite_ok == sum(1 for r in rows if "honest" in r[4])
    gates = [
        ("recall ≥ 0.80", recall >= MIN_RECALL, f"{recall:.0%}"),
        ("abstention 100%", abstention == 1.0, f"{abstention:.0%}"),
        ("adversarial ≥ 0.80", adversarial >= MIN_ADVERSARIAL,
         f"{adversarial:.0%}"),
        ("citation integrity", honest_ok and cite_bad == 1,
         f"{cite_ok} honest · {cite_bad} fabricated-rejected"),
        ("feedback stored", bool(n_feedback), f"{n_feedback} entries"),
    ]
    fails = [g for g in gates if not g[1]]

    lines = ["# Conversational Eval Report — #210\n",
             (f"corpus: {len(pages)} pages · questions: "
              f"{len(rows)} · intents: {len(per_intent)}\n"),
             "| question | intent | verdict | ms | citations |",
             "|---|---|---|---|---|"]
    lines += [f"| {q_[:60]} | {i} | {v} | {ms_} | {c_} |"
              for q_, i, v, ms_, c_, _a in rows]
    lines += ["", "## per-intent\n", "| intent | pass/n | p50 ms |",
              "|---|---|---|"]
    import statistics
    for i, st in sorted(per_intent.items()):
        lines.append(f"| {i} | {st['pass']}/{st['n']} | "
                     f"{int(statistics.median(st['ms']))} |")
    lines += ["", "## gates\n", "| gate | verdict | value |",
              "|---|---|---|"]
    lines += [f"| {n_} | {'PASS' if ok else 'FAIL'} | {v} |"
              for n_, ok, v in gates]
    verdict = "GREEN" if not fails else f"RED — {len(fails)} gate(s)"
    lines += ["", f"**verdict: {verdict}**"]
    Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nreport → {args.report}\n" + "\n".join(
        f"  {n_}: {v} ({'PASS' if ok else 'FAIL'})"
        for n_, ok, v in gates))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
