"""#257 IDE-faithful e2e — two stdio MCP server processes spawned exactly
the way `register` writes them into an IDE's mcp.json
(`python -m local_knowledge.server_local`), each proxying to a DIFFERENT
daemon slot via KNOWLEDGE_DAEMON_HOME (what a separate venv install gets
from its sys.prefix key), over ONE shared workdir index.

Proves the user's scenario end-to-end at the same protocol layer an IDE
uses: IDE B opens its session ('chat open') and connects BEFORE any index
exists; IDE A crawls and publishes; B's very next query — same open
session, no reload, no reconnect — sees A's snapshot, including a later
refresh publish.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KNOWLEDGE_E2E") != "1",
    reason="e2e sessions run under KNOWLEDGE_E2E=1 (CI e2e leg)")


def _corpus(root: Path, canary: str | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "runbook.md").write_text(
        "# Deployment runbook\n\nThe oncall runbook covers rollback steps, "
        "escalation owners, and the production cutover checklist.\n" * 20)
    (root / "notes.txt").write_text(
        "incident escalation matrix and handover notes for the release "
        "train and the staging environment verification\n" * 20)
    if canary:
        (root / "canary.md").write_text(
            f"postmortem action items {canary} tracked under the change "
            "advisory board review cadence\n" * 20)


def _env(shared: Path, workdir: Path, daemon_home: Path) -> dict:
    e = dict(os.environ,
             KNOWLEDGE_FAKE_EMBEDDINGS="1",
             KNOWLEDGE_SHARED_HOME=str(shared),
             LOCAL_KNOWLEDGE_WORKDIR=str(workdir),
             LOCAL_KNOWLEDGE_DAEMON="1",
             KNOWLEDGE_DAEMON_HOME=str(daemon_home))
    e.pop("PYTHONPATH", None)          # the venv resolves its own packages
    return e


async def _call(sess, tool: str, args: dict) -> dict:
    res = await sess.call_tool(tool, args)
    out = None
    if getattr(res, "structuredContent", None):
        out = res.structuredContent
    else:
        for c in res.content:
            if getattr(c, "type", "") == "text":
                try:
                    out = json.loads(c.text)
                except (json.JSONDecodeError, TypeError):
                    out = {"text": c.text}
                break
    if isinstance(out, dict) and set(out) == {"result"}:
        out = out["result"]
    return out or {}


async def _await_done(sess, handle: str, job: str, timeout=120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        p = await _call(sess, "crawl_step",
                        {"scope_handle": handle, "job_id": job})
        if p.get("state") not in (None, "running", "starting"):
            return p
        await asyncio.sleep(0.5)
    raise AssertionError(f"job {job} never left running")


def _daemon_log_tail(*homes: Path) -> str:
    """The daemons' own logs hold the worker traceback that job['errors']
    only summarizes — fold the tails into the assert message so CI's
    failure report carries the root cause (artifact upload only covers
    ~/.knowledge/daemon, not pytest tmp homes)."""
    out = []
    for home in homes:
        log = Path(home) / "daemon.log"
        try:
            out.append(f"--- {log} ---\n" + "\n".join(
                log.read_text(encoding="utf-8",
                              errors="replace").splitlines()[-40:]))
        except OSError as e:
            out.append(f"--- {log} unreadable: {e}")
    return "\n".join(out)


def test_stdio_two_installs_share_index(tmp_path):
    """IDE B (daemon slot B, session held open) → IDE A (slot B) crawls →
    B's next query sees it; A's refresh publish is next-query visible."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    corpus = tmp_path / "corpus"
    _corpus(corpus)
    shared, workdir = tmp_path / "shared", tmp_path / "workdir"
    srv = ["-m", "local_knowledge.server_local"]
    pa = StdioServerParameters(
        command=sys.executable, args=srv,
        env=_env(shared, workdir, tmp_path / "daemonA"))
    pb = StdioServerParameters(
        command=sys.executable, args=srv,
        env=_env(shared, workdir, tmp_path / "daemonB"))

    async def go():
        # IDE B boots first — its session stays open like a chat window
        async with stdio_client(pb) as (rb, wb), ClientSession(rb, wb) as B:
            await B.initialize()
            hB = (await _call(B, "connect",
                              {"root": str(corpus)}))["scope_handle"]
            stB = await _call(B, "status", {})
            pre = await _call(B, "search",
                              {"scope_handle": hB,
                               "question": "who owns the rollback steps"})
            assert not pre.get("evidence"), pre     # nothing indexed yet

            # IDE A — a separate install: own stdio server, own daemon
            async with stdio_client(pa) as (ra, wa), \
                    ClientSession(ra, wa) as A:
                await A.initialize()
                stA = await _call(A, "status", {})
                assert stA["paths"]["daemon_home"] != \
                    stB["paths"]["daemon_home"]
                assert stA["paths"]["index_root"] == \
                    stB["paths"]["index_root"]
                hA = (await _call(A, "connect",
                                  {"root": str(corpus)}))["scope_handle"]
                assert hA == hB            # same root → same scope handle
                job = (await _call(A, "start_crawl",
                                   {"scope_handle": hA}))["job_id"]
                p1 = await _await_done(A, hA, job)
                logs1 = _daemon_log_tail(tmp_path / "daemonA",
                                         tmp_path / "daemonB")
                assert p1["state"] == "done", f"{p1}\n{logs1}"

            # B's next query on the still-open session — no reconnect
            hits = await _call(B, "search",
                               {"scope_handle": hB,
                                "question": "who owns the rollback steps"})
            assert hits.get("evidence"), hits

            # A publishes again; B's very next query sees the new snapshot
            canary = "xyzzy-canary-stdio-4"
            _corpus(corpus, canary)
            async with stdio_client(pa) as (ra, wa), \
                    ClientSession(ra, wa) as A:
                await A.initialize()
                job2 = (await _call(A, "start_crawl",
                                    {"scope_handle": hA,
                                     "refresh": True}))["job_id"]
                p2 = await _await_done(A, hA, job2)
                logs2 = _daemon_log_tail(tmp_path / "daemonA",
                                         tmp_path / "daemonB")
                assert p2["state"] == "done", f"{p2}\n{logs2}"
            hits2 = await _call(B, "search",
                                {"scope_handle": hB, "question": canary})
            assert canary in json.dumps(hits2), hits2

    asyncio.run(go())
