#!/usr/bin/env python3
"""Real-Confluence-Cloud e2e — the entire user journey against a live
atlassian.net site with real ONNX models (#211 cloud leg).

    python scripts/e2e_cloud_real.py [--cli <path>] [--root-id <id>]

Credentials: ~/.config/agents/cloud.env (see seed_cloud.py). The run is
fully sandboxed — CONF_KNOWLEDGE_HOME + KNOWLEDGE_SHARED_HOME point at a
tmp dir so the user's real daemon/index/profile are never touched.

Asserts (hard gate, exit 1 on any FAIL):
  setup --env cloud (headless, token via stdin — never a CLI arg)
  doctor ok → connect(root) → crawl 200-page real tree to done
  canary recall: CONFLUENCE_CANARY_FACT_4482 surfaced with reranker scores
  real viewpage.action citations, verify_citations 100% pass
  fabricated citation 100% rejected, undocumented topic → abstention
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _e2e_common import call, check, fresh_venv

ENV_FILE = Path.home() / ".config/agents/cloud.env"
CANARY = "CONFLUENCE_CANARY_FACT_4482"


def load_env() -> dict:
    if not ENV_FILE.exists():
        sys.exit(f"SKIP — no {ENV_FILE} (real-cloud leg needs "
                 "credentials; the fake-DC legs cover CI)")
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def run(argv, env, ok_msg, timeout=600):
    p = subprocess.run(argv, check=False, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    if p.returncode != 0:
        print(f"FAIL: {' '.join(map(str, argv))}\n{p.stdout[-1200:]}\n"
              f"{p.stderr[-1200:]}", flush=True)
        sys.exit(1)
    print("PASS", ok_msg, flush=True)
    return p


def venv_bin(d: Path) -> Path:
    return d / ("Scripts" if os.name == "nt" else "bin")


async def mcp(url: str, token: str, fn) -> None:
    """Drive the daemon's http surface exactly like the IDE does."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (r, w, _), \
            ClientSession(r, w) as s:
        await s.initialize()
        await fn(s)


def http_meta(shared_home: Path, prefix: str,
              timeout: float = 30) -> dict:
    """Wait for the sandboxed daemon's http.json + a bound listener."""
    import socket
    deadline = time.monotonic() + timeout
    daemon_root = shared_home / "daemon"
    while time.monotonic() < deadline:
        metas = list(daemon_root.glob("*/http.json")) \
            if daemon_root.exists() else []
        for f in metas:
            meta = json.loads(f.read_text())
            try:
                with socket.socket() as sk:
                    sk.settimeout(0.5)
                    sk.connect(("127.0.0.1", meta["port"]))
                return meta
            except OSError:
                pass
        time.sleep(0.3)
    raise SystemExit("sandboxed daemon endpoint never came up")


import time


async def journey(s, ctx) -> None:
    lat = {}

    async def timed(name, tool, args=None, t=30):
        t0 = time.monotonic()
        out = await call(s, tool, args, timeout=t)
        lat[tool] = time.monotonic() - t0
        return out

    tools = {t.name for t in (await s.list_tools()).tools}
    check({"status", "connect", "start_crawl", "wait_for_crawl",
           "search", "get_evidence", "verify_citations",
           "submit_feedback", "feedback_report"} <= tools,
          "full tool set on http")

    # ---- connect + crawl the real tree ----
    st = await timed("status", "status")
    check(st["configured"], "cloud profile configured")
    c = await timed("connect", "connect",
                    {"root": ctx["root"]})
    scope = c["scope_handle"]
    check(scope.startswith("scope:"), "connect returned a scope", c)
    j = await timed("start_crawl", "start_crawl",
                    {"scope_handle": scope})
    jid = j["job_id"]
    # long crawl (~180 real pages) — wait_for_crawl caps at 45s per call
    deadline = time.monotonic() + 2700
    while True:
        w = await timed("wait_for_crawl", "wait_for_crawl",
                        {"scope_handle": scope, "job_id": jid,
                         "wait_s": 45}, t=50)
        if w["state"] in ("done", "failed"):
            break
        if time.monotonic() > deadline:
            sys.exit("crawl did not finish in 45 min — dense corpus "
                     "is slow by design")
    check(w["state"] == "done", "real-cloud crawl completed",
          f"errors={w.get('errors')}")
    print(f"  indexed {w.get('pages_done')} pages", flush=True)
    st = w.get("stats")
    if st:
        tot = sum(v for k, v in st.items() if k.endswith("_ms")) or 1
        ctx["stats"] = {k[:-3]: f"{v / 1000:.1f}s ({v * 100 // tot}%)"
                        for k, v in st.items() if k.endswith("_ms")}
        ctx["stats"]["pages/s"] = st.get("pages_per_s")
        ctx["stats"]["battery_saver"] = st.get("battery_saver")
    min_pages = int(args[args.index("--min-pages") + 1]) \
        if "--min-pages" in args else 150
    check(w.get("pages_done", 0) >= min_pages,
          f"covered most of the subtree (≥{min_pages} expected)",
          w.get("pages_done"))

    # ---- canary recall: real reranker scores + real URL citations ----
    res = await timed("search", "search",
                      {"scope_handle": scope,
                       "question": f"what is the canary codeword {CANARY}"
                                   " and what are the rollout steps?"}, t=90)
    ev = res["evidence"]
    check(ev, "search returned evidence")
    titles = [e["page_title"] for e in ev]
    check(any("Canary" in t for t in titles),
          "canary page surfaced", titles)
    check(any(CANARY in e["text"] for e in ev),
          "canary fact in evidence text")
    check(all(isinstance(e["score"], (int, float)) for e in ev),
          "reranker scored every chunk")
    check(all("atlassian.net" in e["page_url"]
              or "viewpage.action" in e["page_url"] for e in ev),
          "real cloud citations", [e["page_url"] for e in ev])
    check(res["coverage"] == "complete",
          "coverage = complete (no crawl errors)", res["coverage"])

    # get_evidence on the top chunks — full text round-trips
    ge = await timed("get_evidence", "get_evidence",
                     {"scope_handle": scope,
                      "chunk_ids": [ev[0]["chunk_id"], ev[1]["chunk_id"]]})
    check(len(ge["chunks"]) == 2, "get_evidence round-trips chunks")

    # ---- citation integrity + fraud ----
    good = [{"claim": "the canary codeword",
             "chunk_id": ev[0]["chunk_id"],
             "quote": ev[0]["text"][:60]}]
    ok = await timed("verify_citations", "verify_citations",
                     {"scope_handle": scope,
                      "evidence_set": res["evidence_set"],
                      "mappings": good})
    check(ok["ok"] is True and not ok.get("problems"),
          "honest citations verify", ok)
    fraud = [{"claim": "fabricated",
              "chunk_id": "chunk:deadbeef:0",
              "quote": "a sentence that never appeared"}]
    bad = await timed("verify_citations+1", "verify_citations",
                      {"scope_handle": scope,
                       "evidence_set": res["evidence_set"],
                       "mappings": fraud})
    check(bad["ok"] is False and bad.get("violations"),
          "fabricated citation rejected", bad)

    # ---- abstention on an undocumented topic ----
    miss = await timed("search", "search",
                       {"scope_handle": scope,
                        "question": "what is the company policy on "
                                    "remote work in the Bahamas?"}, t=90)
    honest = (not miss["evidence"]
              or all(e.get("low_confidence") for e in miss["evidence"]))
    check(honest, "undocumented topic → abstention, no hallucination",
          [e.get("score") for e in miss["evidence"]])

    # ---- feedback loop ----
    fb = await timed("submit_feedback", "submit_feedback",
                     {"scope_handle": scope, "question": "canary?",
                      "correction": "rollout steps are in the "
                                    "canary guide"})
    check(fb.get("ok") or fb.get("saved") or fb.get("stored"),
          "feedback recorded", fb)
    rep = await timed("feedback_report", "feedback_report",
                      {"scope_handle": scope})
    check(isinstance(rep, dict), "feedback report readable")

    ctx["lat"] = lat


def main() -> int:
    args = sys.argv[1:]
    cli_arg = args[args.index("--cli") + 1] if "--cli" in args else None
    root = args[args.index("--root-id") + 1] if "--root-id" in args \
        else "491521"  # Engineering Knowledge Base root — see seed_cloud.py
    cloud = load_env()
    site = cloud["CONFLUENCE_CLOUD_SITE"].rstrip("/")
    email = cloud["CONFLUENCE_CLOUD_EMAIL"]

    sandbox = Path(tempfile.mkdtemp(prefix="ck-cloud-"))
    home = sandbox / "home"; home.mkdir()
    env = {**os.environ,
           "CONF_KNOWLEDGE_HOME": str(home / ".confluence-knowledge"),
           "CONF_KNOWLEDGE_WORKDIR": str(home / "work"),
           "KNOWLEDGE_SHARED_HOME": str(home / ".knowledge"),
           # this leg exercises the shared-daemon http surface — opt in
           # explicitly (local-first is the default since #285)
           "CONF_KNOWLEDGE_DAEMON": "1",
           # real ONNX models from the dev store — no re-download, and
           # this leg must never fall back to fake embeddings
           "KNOWLEDGE_MODELS_DIR":
               os.environ.get("KNOWLEDGE_MODELS_DIR")
               or str(Path.home() / ".knowledge" / "models")}
    print(f"sandbox: {sandbox}")

    # venv: --cli reuses an installed package (dev verification); default
    # builds a fresh one from source (the release-gate path)
    if cli_arg:
        cli = Path(cli_arg)
        vpy = cli.parent / "python"
    else:
        vdir = sandbox / "venv"
        fresh_venv(vdir)
        vpy = venv_bin(vdir) / ("python.exe" if os.name == "nt"
                                else "python")
        src = Path(__file__).resolve().parents[1]
        run([str(vpy), "-m", "pip", "install", f"{src}[models]"], env,
            "venv + install confluence-knowledge[models]")
        cli = venv_bin(vdir) / ("confluence-knowledge.exe"
                                if os.name == "nt"
                                else "confluence-knowledge")

    # headless cloud setup — token through stdin, never argv/logs
    p = subprocess.run(
        [str(cli), "setup", "--env", "cloud", "--site", site,
         "--email", email, "--token-stdin"],
        input=cloud["CONFLUENCE_CLOUD_TOKEN"] + "\n",
        env=env, capture_output=True, text=True, timeout=60,
        check=False)
    check(p.returncode == 0, "setup --env cloud stored credentials",
          p.stdout + p.stderr)

    # doctor must pass against the live site (models may still be
    # preparing — doctor reports but doesn't fail on that)
    p = subprocess.run([str(cli), "doctor"], env=env,
                       capture_output=True, text=True, timeout=120,
                       check=False)
    print(p.stdout[-1500:])
    check(p.returncode in (0, 4), "doctor ran", p.stdout[-300:])

    # register → spawns the sandboxed shared daemon
    # workspace-scoped registration only — user IDE configs untouched
    run([str(cli), "register", "--ide", "vscode",
         "--workspace", str(sandbox)],
        env, "register: workspace mcp.json written + daemon spawned")

    meta = http_meta(home / ".knowledge", "confluence-knowledge")
    url = f"http://127.0.0.1:{meta['port']}/confluence-knowledge/mcp"
    print(f"daemon endpoint: {url}")

    ctx = {"root": f"{site}/wiki/pages/viewpage.action?pageId={root}"}
    asyncio.run(mcp(url, meta["token"],
                    lambda s: journey(s, ctx)))
    print("\nlatencies:", {k: f"{v:.1f}s" for k, v in ctx["lat"].items()})
    if ctx.get("stats"):
        print("crawl phases:", ctx["stats"])
    print("\nREAL CLOUD E2E OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
