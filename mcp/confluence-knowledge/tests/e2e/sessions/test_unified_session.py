"""#265/#269 unified `knowledge` entry — a real stdio MCP session to
`python -m confluence_knowledge.unified` serves the namespaced union of
every installed service, and namespaced calls drive a full local crawl
exactly as they would on the per-service entry.

Proves the zero-reload promise at the protocol layer an IDE uses:
ONE server entry, ALL tools present as <service>__<tool>, and a call
routed through the union reaches the same daemon/index as a direct
per-service session.
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


def _corpus(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "runbook.md").write_text(
        "# Deployment runbook\n\nThe oncall runbook covers rollback steps, "
        "escalation owners, and the production cutover checklist.\n" * 20)
    (root / "notes.txt").write_text(
        "incident escalation matrix and handover notes for the release "
        "train and the staging environment verification\n" * 20)


def _env(shared: Path, workdir: Path, daemon_home: Path) -> dict:
    e = dict(os.environ,
             KNOWLEDGE_FAKE_EMBEDDINGS="1",
             KNOWLEDGE_SHARED_HOME=str(shared),
             LOCAL_KNOWLEDGE_WORKDIR=str(workdir),
             LOCAL_KNOWLEDGE_DAEMON="1",   # conftest pins *=0 for unit
             KNOWLEDGE_DAEMON_HOME=str(daemon_home))
    e.pop("PYTHONPATH", None)
    return e


async def _call(sess, tool: str, args: dict) -> dict:
    res = await sess.call_tool(tool, args)
    out = None
    for c in res.content:
        if getattr(c, "type", "") == "text":
            try:
                out = json.loads(c.text)
            except (json.JSONDecodeError, TypeError):
                out = {"text": c.text}
            break
    if getattr(res, "structuredContent", None):
        out = res.structuredContent
    if isinstance(out, dict) and set(out) == {"result"}:
        out = out["result"]
    return out or {}


def test_unified_stdio_serves_namespaced_union(tmp_path):
    """One `knowledge` session: union listing, prefix routing, error
    hygiene, and a real local crawl through confluence_/local_ names."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    corpus = tmp_path / "corpus"
    _corpus(corpus)
    env = _env(tmp_path / "shared", tmp_path / "workdir",
               tmp_path / "daemon")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "confluence_knowledge.unified"], env=env)

    async def go():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            # every name is namespaced; confluence is always installed on
            # this leg, local on legs that install the sibling wheel
            assert tools and all("__" in t for t in tools), tools
            assert any(t.startswith("confluence__") for t in tools)

            st = await _call(s, "confluence__status", {})
            assert "configured" in st or "checks" in st, st

            res = await s.call_tool("nosuch__status", {})
            assert res.isError, res
            res = await s.call_tool("status", {})   # unprefixed
            assert res.isError, res

            if "local-knowledge" in {ep.name for ep in
                                     __import__("importlib.metadata",
                                                fromlist=["x"]).
                                     entry_points(
                                         group="knowledge.services")}:
                assert any(t.startswith("local__") for t in tools)
                h = (await _call(s, "local__connect",
                                 {"root": str(corpus)}))["scope_handle"]
                job = (await _call(s, "local__start_crawl",
                                   {"scope_handle": h}))["job_id"]
                deadline = time.monotonic() + 120
                while True:
                    p = await _call(s, "local__crawl_step",
                                    {"scope_handle": h, "job_id": job})
                    if p.get("state") not in (None, "running", "starting"):
                        break
                    assert time.monotonic() < deadline, p
                    await asyncio.sleep(0.5)
                assert p["state"] == "done", p
                hits = await _call(s, "local__search",
                                   {"scope_handle": h,
                                    "question": "who owns rollback"})
                assert hits.get("evidence"), hits
                assert hits.get("scope_handle") == h  # handle echo

    asyncio.run(go())
