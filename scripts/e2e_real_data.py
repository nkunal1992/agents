#!/usr/bin/env python3
"""#211 real-data e2e — BOTH services on ONE shared daemon, real ONNX
models, real on-disk data. The deterministic counterpart of
e2e_cloud_real.py (this leg is CI-runnable; the cloud leg is not).

  Workspace    /tmp/e2e_real_data_test (or --workspace <dir>)
  local_docs/  deployment_guide.md, architecture_spec.txt,
               incident_playbook.md  (+ LOCAL_CANARY_FACT_9921)
  fake DC      15-page in-process server, root page 1001
               (+ CONFLUENCE_CANARY_FACT_4482)

  Journey      venv installs BOTH packages → ONE shared daemon mounts
               /confluence-knowledge/mcp + /local-knowledge/mcp →
               connect → crawl → canary recall → get_evidence →
               verify_citations (pass + fabricated rejected) →
               abstention → feedback → REAL_DATA_E2E_TEST_REPORT.md

  --cli <confluence-knowledge> skips the venv (dev verification against
  an existing install — the release gate always builds fresh).
  KNOWLEDGE_MODELS_DIR / CK_MODEL_CACHE point at real model assets; the
  script refuses to run under KNOWLEDGE_FAKE_EMBEDDINGS.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CK_SRC = REPO / "mcp" / "confluence-knowledge"
LK_SRC = REPO / "mcp" / "local-knowledge"
sys.path.insert(0, str(CK_SRC / "scripts"))
from _e2e_common import call, check, fresh_venv
from _fake_dc_work import FakeTree, Hostility, serve

LOCAL_CANARY = "LOCAL_CANARY_FACT_9921"
CONF_CANARY = "CONFLUENCE_CANARY_FACT_4482"
DC_TOKEN = "stress-token-canary"
REPORT = Path("REAL_DATA_E2E_TEST_REPORT.md")
LOG: list[str] = []


def note(msg: str) -> None:
    print(msg, flush=True)
    LOG.append(msg)


def venv_bin(d: Path) -> Path:
    return d / ("Scripts" if os.name == "nt" else "bin")


def run(argv, env, msg, timeout=600, check_rc=True):
    p = subprocess.run(argv, check=False, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    if check_rc and p.returncode != 0:
        note(f"FAIL: {' '.join(map(str, argv))}\n{p.stdout[-1200:]}\n"
             f"{p.stderr[-1200:]}")
        sys.exit(1)
    note(f"PASS {msg}")
    return p


# ------------------------------ datasets ----------------------------------

def make_local_docs(root: Path) -> Path:
    docs = root / "local_docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "deployment_guide.md").write_text(
        "# Microservice Deployment Guide\n\n"
        "A five-step rollout for blue/green microservice releases.\n\n"
        "## Step 1 — prepare\nFreeze main, tag release candidate, "
        "verify staging parity.\n\n"
        "## Step 2 — deploy blue\nShip the new image to the blue "
        "environment; run smoke tests.\n\n"
        "## Step 3 — Direct 10% traffic to blue deployment\nHold for "
        "30 minutes; watch error rate (<0.5%) and p99 (<800ms).\n\n"
        "## Step 4 — raise to 50%\nOne hour at half traffic; compare "
        "golden signals against green.\n\n"
        "## Step 5 — full cutover\nRoute 100% to blue; keep green warm "
        "for 24h as rollback. Rollback codeword reference: "
        f"{LOCAL_CANARY}.\n", encoding="utf-8")
    (docs / "architecture_spec.txt").write_text(
        "Architecture Specification — Database Layer\n"
        "============================================\n\n"
        "The primary database writes to a single writer with one hot "
        "standby. Failover triggers after 3000ms heartbeat timeout across "
        "three consecutive missed probes. The standby promotes in ~12s; "
        "clients retry through the connection pool. A consensus lock on "
        "the coordinator prevents split-brain.\n\n"
        "Storage: row-oriented OLTP; nightly snapshots to object "
        "storage, WAL archives every 5 minutes.\n", encoding="utf-8")
    (docs / "incident_playbook.md").write_text(
        "# Incident Playbook\n\n"
        "Sev-1 pages on-call + incident commander; ack within 5 min.\n\n"
        "| Tier | Who | Time |\n|---|---|---|\n"
        "| 1 | on-call engineer | 5 min |\n"
        "| 2 | team lead | 15 min |\n"
        "| 3 | engineering manager | 30 min |\n"
        "| 4 | director | 45 min |\n\n"
        "Blameless postmortem within 72 hours. Runbook index lives in "
        "the ops handbook.\n", encoding="utf-8")
    return docs


def conf_corpus() -> FakeTree:
    """15-page DC-shaped tree rooted at 1001 — engineering release
    procedures; the canary lives on the rollout page."""
    pages = {"1001": {"title": "Engineering Release Procedures",
                      "version": 1, "ancestors": [],
                      "body": "<p>Root of the release procedures space."
                              "</p>"}}
    children = {"1001": []}
    content = [
        ("1002", "Release Calendar",
         "Releases ship Thursday 18:00. Freeze begins Wednesday 12:00."),
        ("1003", "Canary Rollout Guide",
         ("Step 1: deploy blue. Step 2: direct 10% of traffic to blue "
          f"for 30 minutes. Step 3: 50% for an hour. Step 4: full "
          f"cutover. Audit codeword: {CONF_CANARY}. Pause on error "
          "rate >0.5%.")),
        ("1004", "Rollback Playbook",
         ("Rollback restores the prior image tag; migrations stay "
          "backwards-compatible for two releases.")),
        ("1005", "Hotfix Policy",
         ("Hotfixes bypass the window with staff-engineer approval, "
          "behind a defaulted-off flag.")),
        ("1006", "Database Failover Specification",
         ("Failover triggers after 3000ms heartbeat timeout across three "
          "probes; standby promotes in ~12s.")),
        ("1007", "Incident Playbook",
         ("Sev-1: on-call ack in 5 min; escalation ladder 15/30/45 min; "
          "postmortem in 72 hours.")),
        ("1008", "Monitoring Baselines",
         ("Alert thresholds: p99 latency 800ms, error rate 0.5%, "
          "queue depth 5000.")),
        ("1009", "On-Call Handbook",
         ("Weekly rotation, Monday 10:00 handoff with a written "
          "summary.")),
    ]
    for pid, title, text in content:
        pages[pid] = {"title": title, "version": 1,
                      "ancestors": ["1001"],
                      "body": f"<h2>{title}</h2><p>{text}</p>"}
        children["1001"].append(pid)
    # depth: each procedure gets one drill sub-page
    n = 1010
    for parent, title, _ in content[:6]:
        pages[str(n)] = {"title": f"{title} — Drill", "version": 1,
                         "ancestors": ["1001", parent],
                         "body": f"<p>Quarterly drill for {title}: "
                                 "verify the checklist end-to-end in "
                                 "staging.</p>"}
        children.setdefault(parent, []).append(str(n))
        n += 1
    return FakeTree(pages=pages, children=children, root="1001")


# --------------------------- daemon plumbing ------------------------------

def http_meta(shared_home: Path, timeout: float = 40) -> dict:
    deadline = time.monotonic() + timeout
    root = shared_home / "daemon"
    while time.monotonic() < deadline:
        for f in (root.glob("*/http.json") if root.exists() else []):
            try:
                meta = json.loads(f.read_text(encoding="utf-8"))
                with socket.socket() as sk:
                    sk.settimeout(0.5)
                    sk.connect(("127.0.0.1", meta["port"]))
                return meta
            except (OSError, json.JSONDecodeError):
                pass
        time.sleep(0.3)
    raise SystemExit("shared daemon endpoint never came up")


async def mcp(url: str, token: str, fn) -> None:
    from mcp.client.streamable_http import streamablehttp_client

    from mcp import ClientSession
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (r, w, _), \
            ClientSession(r, w) as s:
        await s.initialize()
        await fn(s)


def svc_url(meta: dict, svc: str) -> str:
    return f"http://127.0.0.1:{meta['port']}/{svc}/mcp"


# ------------------------------ journeys ----------------------------------

async def confluence_leg(s, report: dict) -> None:
    tools = {t.name for t in (await s.list_tools()).tools}
    check({"connect", "start_crawl", "wait_for_crawl", "search",
           "get_evidence", "verify_citations"} <= tools,
          "confluence tool set on shared daemon")
    c = await call(s, "connect", {"root": "1001"})
    scope = c["scope_handle"]
    j = await call(s, "start_crawl", {"scope_handle": scope})
    jid = j["job_id"]
    t0 = time.monotonic()
    while True:
        w = await call(s, "wait_for_crawl",
                       {"scope_handle": scope, "job_id": jid,
                        "wait_s": 30}, timeout=40)
        if w["state"] in ("done", "failed"):
            break
        if time.monotonic() - t0 > 300:
            sys.exit("confluence crawl >5min")
    check(w["state"] == "done", "fake-DC crawl done",
          w.get("errors"))
    check(w.get("pages_done", 0) == 15, "all 15 pages indexed",
          w.get("pages_done"))
    report["conf_pages"] = w.get("pages_done")
    report["conf_stats"] = w.get("stats")

    res = await call(s, "search",
                     {"scope_handle": scope,
                      "question": "what are the canary rollout steps "
                                  f"and codeword ({CONF_CANARY})?"}, 90)
    ev = res["evidence"]
    check(ev and any(CONF_CANARY in e["text"] for e in ev),
          "confluence canary retrieved")
    check(any("Canary" in e["page_title"] for e in ev),
          "canary page title", [e["page_title"] for e in ev])
    check(all(isinstance(e["score"], (int, float)) for e in ev),
          "reranker scores present")
    check(all("127.0.0.1" in e["page_url"] for e in ev),
          "DC page-url citations")
    ge = await call(s, "get_evidence",
                    {"scope_handle": scope,
                     "chunk_ids": [ev[0]["chunk_id"]]})
    check(len(ge["chunks"]) == 1, "get_evidence chunk fetch")
    ok = await call(s, "verify_citations",
                    {"scope_handle": scope,
                     "evidence_set": res["evidence_set"],
                     "mappings": [{"claim": "rollout steps",
                                   "chunk_id": ev[0]["chunk_id"],
                                   "quote": ev[0]["text"][:60]}]})
    check(ok["ok"] is True, "honest citations verify", ok)
    bad = await call(s, "verify_citations",
                     {"scope_handle": scope,
                      "evidence_set": res["evidence_set"],
                      "mappings": [{"claim": "fabricated",
                                    "chunk_id": "chunk:zzz:0",
                                    "quote": "invented sentence"}]})
    check(bad["ok"] is False and bad.get("violations"),
          "fabricated confluence citation rejected")
    miss = await call(s, "search",
                      {"scope_handle": scope,
                       "question": "what is the company policy on remote"
                                   " work in the Bahamas?"}, 90)
    check(not miss["evidence"]
          or all(e.get("low_confidence") for e in miss["evidence"]),
          "confluence abstains on undocumented topic")
    report["conf_scores"] = [round(e["score"], 4) for e in ev[:3]]


async def local_leg(s, docs: Path, report: dict) -> None:
    c = await call(s, "connect", {"root": str(docs)})
    scope = c["scope_handle"]
    check(scope.startswith("scope:"), "local connect", c)
    j = await call(s, "start_crawl", {"scope_handle": scope})
    jid = j["job_id"]
    t0 = time.monotonic()
    while True:
        w = await call(s, "wait_for_crawl",
                       {"scope_handle": scope, "job_id": jid,
                        "wait_s": 30}, timeout=40)
        if w["state"] in ("done", "failed"):
            break
        if time.monotonic() - t0 > 240:
            sys.exit("local crawl >4min")
    check(w["state"] == "done", "local docs crawl done",
          w.get("errors"))
    report["local_files"] = w.get("pages_done")
    report["local_stats"] = w.get("stats")

    # one query per doc — a compound question asks the xsmall reranker to
    # score each doc on evidence it only half-contains, which legitimately
    # lands below the noise floor (the abstention floor working as
    # designed, not a retrieval failure)
    res = await call(s, "search",
                     {"scope_handle": scope,
                      "question": "what is the database failover "
                                  "heartbeat timeout?"}, 90)
    ev = res["evidence"]
    titles = {Path(e["page_url"]).name for e in ev}
    check("architecture_spec.txt" in titles,
          "failover doc surfaced", titles)
    res2 = await call(s, "search",
                      {"scope_handle": scope,
                       "question": "what percentage of traffic goes to "
                                   "the blue deployment?"}, 90)
    titles2 = {Path(e["page_url"]).name for e in res2["evidence"]}
    check("deployment_guide.md" in titles2,
          "deploy doc surfaced", titles2)
    check(all(e["page_url"].startswith("file://")
              for e in ev + res2["evidence"]),
          "file:// citations", titles | titles2)
    ok = await call(s, "verify_citations",
                    {"scope_handle": scope,
                     "evidence_set": res["evidence_set"],
                     "mappings": [{"claim": "failover threshold",
                                   "chunk_id": ev[0]["chunk_id"],
                                   "quote": ev[0]["text"][:60]}]})
    check(ok["ok"] is True, "honest local citations verify", ok)
    bad = await call(s, "verify_citations",
                     {"scope_handle": scope,
                      "evidence_set": res["evidence_set"],
                      "mappings": [{"claim": "fabricated",
                                    "chunk_id": "chunk:zzz:9",
                                    "quote": "made up"}]})
    check(bad["ok"] is False and bad.get("violations"),
          "fabricated local citation rejected")
    miss = await call(s, "search",
                      {"scope_handle": scope,
                       "question": "company policy on remote work in the"
                                   " Bahamas?"}, 90)
    check(not miss["evidence"]
          or all(e.get("low_confidence") for e in miss["evidence"]),
          "local abstains on undocumented topic")

    canary = await call(s, "search",
                        {"scope_handle": scope,
                         "question": f"where is {LOCAL_CANARY} "
                                     "referenced?"}, 90)
    check(any(LOCAL_CANARY in e["text"]
              for e in canary["evidence"]), "local canary retrieved")
    report["local_scores"] = [round(e["score"], 4) for e in ev[:3]]


def _phase_table(name: str, st: dict | None) -> str:
    if not st:
        return f"### {name}: no stats\n"
    rows = [(k, round(v / 1000, 1)) for k, v in st.items()
            if k.endswith("_ms")]
    tot = sum(v for _, v in rows) or 1
    out = (f"### {name}\n| phase | s | % |\n|---|---|---|\n")
    for k, v in rows:
        out += f"| {k[:-3]} | {v} | {v * 100 // tot}% |\n"
    out += (f"| pages | {st.get('pages')} | |\n"
            f"| chunks | {st.get('chunks')} | |\n"
            f"| pages/s | {st.get('pages_per_s')} | |\n"
            f"| battery_saver | {st.get('battery_saver')} | |\n\n")
    return out


# -------------------------------- main ------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default="/tmp/e2e_real_data_test"
                    if os.name != "nt" else None)
    ap.add_argument("--cli", default=None)
    ap.add_argument("--report", default=str(REPORT))
    args = ap.parse_args()
    check(os.environ.get("KNOWLEDGE_FAKE_EMBEDDINGS") != "1",
          "real-data gate refuses KNOWLEDGE_FAKE_EMBEDDINGS=1")

    ws = Path(args.workspace or tempfile.mkdtemp(prefix="e2e-real-"))
    ws.mkdir(parents=True, exist_ok=True)
    report = {"workspace": str(ws), "phases": []}
    sandbox = ws / "sandbox"
    # the default workspace is a fixed path reused across runs — stale
    # venvs/homes/index DBs from earlier runs must not poison this one
    if sandbox.exists():
        shutil.rmtree(sandbox)
    home = sandbox / "home"; home.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "CONF_KNOWLEDGE_HOME": str(home / ".confluence-knowledge"),
           "CONF_KNOWLEDGE_WORKDIR": str(home / "work"),
           "LOCAL_KNOWLEDGE_HOME": str(home / ".local-knowledge"),
           "LOCAL_KNOWLEDGE_WORKDIR": str(home / "work-local"),
           "KNOWLEDGE_SHARED_HOME": str(home / ".knowledge"),
           "CONF_KNOWLEDGE_TOKEN": DC_TOKEN,
           # this script exercises the shared-daemon mount — opt in
           # explicitly (local-first is the default since #285)
           "KNOWLEDGE_DAEMON": "1",
           "KNOWLEDGE_MODELS_DIR":
               os.environ.get("KNOWLEDGE_MODELS_DIR")
               or os.environ.get("CK_MODEL_CACHE")
               or str(Path.home() / ".knowledge" / "models"),
           "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    note(f"workspace: {ws}")

    docs = make_local_docs(ws)
    note("PASS local_docs corpus written (3 files + canary)")

    _srv, base, hits = serve(conf_corpus(), Hostility())
    note(f"PASS fake DC serving 15 pages at {base} (root 1001)")

    if args.cli:
        cli = Path(args.cli)
        vpy = cli.parent / "python"
    else:
        vdir = sandbox / "venv"
        fresh_venv(vdir)
        vpy = venv_bin(vdir) / ("python.exe" if os.name == "nt" else "python")
        cli = venv_bin(vdir) / ("confluence-knowledge.exe"
                                if os.name == "nt" else "confluence-knowledge")
        run([str(vpy), "-m", "pip", "install",
             f"{CK_SRC}[models]", str(LK_SRC)], env,
            "venv + install BOTH packages (one shared daemon)")

    # datacenter profile → fake DC; env-var credential path (no keyring
    # needed under the sandboxed HOME)
    ck_home = home / ".confluence-knowledge"
    ck_home.mkdir(parents=True, exist_ok=True)
    (ck_home / "profile.json").write_text(json.dumps(
        {"env": "datacenter", "base_url": base, "email": None,
         "credential_ref": "e2e"}), encoding="utf-8")

    run([str(cli), "doctor"], env, "confluence doctor",
        check_rc=False)
    lcli = (cli.parent / "local-knowledge") if args.cli else         venv_bin(vdir) / ("local-knowledge.exe" if os.name == "nt"
                          else "local-knowledge")
    if lcli.exists():
        run([str(lcli), "doctor"], env, "local doctor", check_rc=False)

    ws_ide = sandbox / "ws"; ws_ide.mkdir(exist_ok=True)
    run([str(cli), "register", "--ide", "vscode", "--workspace",
         str(ws_ide)], env, "register (shared daemon spawned)")
    if lcli.exists():
        run([str(lcli), "register", "--ide", "vscode", "--workspace",
             str(ws_ide)], env, "local register (same daemon)")

    meta = http_meta(home / ".knowledge")
    note(f"shared daemon endpoint: 127.0.0.1:{meta['port']}")

    async def both(s1):
        await confluence_leg(s1, report)

    async def both_local(s2):
        await local_leg(s2, docs, report)

    asyncio.run(mcp(svc_url(meta, "confluence-knowledge"), meta["token"],
                    both))
    asyncio.run(mcp(svc_url(meta, "local-knowledge"), meta["token"],
                    both_local))
    note(f"fake DC request log: {len(hits)} requests")

    Path(args.report).write_text(
        "# Real-Data E2E Report\n\n"
        f"- workspace `{ws}`\n"
        f"- confluence pages indexed: {report.get('conf_pages')}\n"
        f"- local files indexed: {report.get('local_files')}\n"
        f"- canary scores (conf): {report.get('conf_scores')}\n"
        f"- search scores (local): {report.get('local_scores')}\n"
        f"- fake-DC requests: {len(hits)}\n\n"
        "## Crawl phase timing\n\n"
        + _phase_table("confluence", report.get("conf_stats"))
        + _phase_table("local", report.get("local_stats"))
        + "\n## Log\n```\n" + "\n".join(LOG) + "\n```\n", encoding="utf-8")
    note(f"report → {args.report}")
    note("\nREAL DATA E2E OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
