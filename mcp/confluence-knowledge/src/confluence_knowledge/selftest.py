"""Post-install self-test — runs in a subprocess on the NEW code.

`<cli> update` runs this after install + prepare-models; a failure rolls
the environment back. Also exposed as `<cli> selftest` for manual use
("is my install actually working?").

Stages (each timed, recorded):
  import_server — the server module imports
  tools         — the MCP tool list covers the core contract
  health        — deps importable + models prepared
  mini_rag      — real models: build a tiny index, retrieve the right doc,
                  verify citations accept a real quote + reject a fake one

Exit 0 when every stage passes, 1 otherwise; JSON report on stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import tempfile
import time
from pathlib import Path

CORE_TOOLS = {"status", "connect", "start_crawl", "crawl_step", "search",
              "get_evidence", "verify_citations", "submit_feedback",
              "feedback_report"}

MINI_DOCS = [
    {"page_id": "p1", "title": "Deploy Windows",
     "html": "<p>Production deploys happen only on Thursdays after 18:00. "
             "The gateway must be restarted before every deploy.</p>"},
    {"page_id": "p2", "title": "Onboarding",
     "html": "<p>New hires collect their laptops from the IT desk on their "
             "first day and sign the equipment form.</p>"},
]
MINI_QUERY = "when are production deploys allowed?"
MINI_ANSWER_WORDS = ("thursdays", "18:00")


def _stage(name: str):
    def deco(fn):
        fn.stage_name = name
        return fn
    return deco


def run(home: Path, server_module: str) -> dict:
    stages = []
    t0 = time.monotonic()

    # 1 — server module imports
    try:
        mod = importlib.import_module(server_module)
        stages.append({"stage": "import_server", "ok": True,
                       "detail": server_module})
    except Exception as e:  # noqa: BLE001 — report, don't crash
        return _report(stages + [{"stage": "import_server", "ok": False,
                                  "detail": f"{type(e).__name__}: {e}"}], t0)

    # 2 — MCP tools registered
    try:
        tools = asyncio.run(mod.mcp.list_tools())
        names = {t.name for t in tools}
        missing = sorted(CORE_TOOLS - names)
        stages.append({"stage": "tools", "ok": not missing,
                       "detail": f"{len(names)} tools"
                                 + (f" — missing: {missing}" if missing
                                    else "")})
    except Exception as e:  # noqa: BLE001
        stages.append({"stage": "tools", "ok": False,
                       "detail": f"{type(e).__name__}: {e}"})

    # 3 — health checks (deps + models must be green)
    try:
        from .health import health_checks
        checks = health_checks(home, home, "selftest", "selftest",
                               index_root=None)
        from .retrieval import active_embed_spec
        bad = [c["flag"] for c in checks
               if not c["ok"] and c["flag"] in
               {"deps", "models_complete"} | {
                   f"model_{k}" for k in
                   (active_embed_spec()["lock_key"],
                    "rerank", "rerank_light", "sparse")}]
        stages.append({"stage": "health", "ok": not bad,
                       "detail": "red: " + ", ".join(bad) if bad
                                 else "deps + models ok"})
    except Exception as e:  # noqa: BLE001
        stages.append({"stage": "health", "ok": False,
                       "detail": f"{type(e).__name__}: {e}"})

    # 4 — mini-RAG with the real models (catches ONNX/fastembed breakage)
    try:
        stages.append(_mini_rag(home))
    except Exception as e:  # noqa: BLE001
        stages.append({"stage": "mini_rag", "ok": False,
                       "detail": f"{type(e).__name__}: {e}"})

    for s in stages:
        s.setdefault("seconds", round(time.monotonic() - t0, 1))
    return _report(stages, t0)


def _mini_rag(home: Path) -> dict:
    from .extract import extract_chunks
    from .index import Index
    from .retrieval import (
        LocalEmbedder,
        LocalReranker,
        LocalSparseEmbedder,
        SparseUnavailable,
        check_citations,
        retrieve,
    )
    with tempfile.TemporaryDirectory() as td:
        idx = Index(Path(td) / "selftest.db")
        emb = LocalEmbedder(home)
        sparse = None
        try:
            sparse = LocalSparseEmbedder(home)
        except SparseUnavailable:
            pass                    # platform wheel gap — dense+rerank
        for doc in MINI_DOCS:
            chunks, _ = extract_chunks(doc["html"])
            for c in chunks:
                c.chunk_id = f"{doc['page_id']}:{c.ordinal}"
            texts = [f"{doc['title']}\n{c.heading_path}\n{c.text}"
                     for c in chunks]
            vecs = emb.embed(texts)
            sp_vecs = sparse.embed(texts) if sparse else None
            page = {"page_id": doc["page_id"], "title": doc["title"],
                    "url": f"selftest://{doc['page_id']}", "version": 1}
            idx.upsert_page(page, chunks, vecs, sparse=sp_vecs)
        idx.publish({d["page_id"] for d in MINI_DOCS})
        ev = retrieve(idx, MINI_QUERY, emb, LocalReranker(home),
                      sparse=sparse)
        idx.close()
    if not ev:
        return {"stage": "mini_rag", "ok": False, "detail": "no evidence"}
    top = ev[0]
    if top["page_title"] != "Deploy Windows":
        return {"stage": "mini_rag", "ok": False,
                "detail": f"wrong top hit: {top['page_title']}"}
    quote = "Production deploys happen only on Thursdays after 18:00"
    mappings = [{"claim": "Deploys happen on Thursdays",
                 "chunk_id": top["chunk_id"], "quote": quote}]
    ok_cite = check_citations(ev, mappings)["ok"]
    mappings[0]["quote"] = "a quote that never appears in the chunk"
    ok_reject = not check_citations(ev, mappings)["ok"]
    ok = ok_cite and ok_reject
    return {"stage": "mini_rag", "ok": ok,
            "detail": (f"top={top['page_title']} "
                       f"cite={'ok' if ok_cite else 'fail'} "
                       f"reject-fake={'ok' if ok_reject else 'missed'}")}


def _report(stages: list[dict], t0: float) -> dict:
    return {"ok": all(s["ok"] for s in stages),
            "seconds": round(time.monotonic() - t0, 1),
            "stages": stages}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="selftest")
    ap.add_argument("--home", required=True)
    ap.add_argument("--server-module", required=True)
    args = ap.parse_args(argv)
    rep = run(Path(args.home), args.server_module)
    print(json.dumps(rep, indent=2))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
