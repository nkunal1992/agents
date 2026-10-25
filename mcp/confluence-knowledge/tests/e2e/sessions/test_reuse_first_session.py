"""#263 — the reported new-chat gap, at real-process fidelity: an earlier
session already crawled; a FRESH stdio session (new chat window, empty
in-memory scope map) must reuse the persisted index instead of demanding
the root again.

A new chat's protocol-level answer is `status().indexed_scopes` plus an
empty-handle `search` that auto-resolves the single on-disk index and
echoes the usable scope handle for `get_evidence`/`verify_citations`.
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
        "# Deployment runbook\n\nThe oncall runbook covers rollback "
        "steps and escalation owners.\n" * 20)


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
    for c in res.content:
        if getattr(c, "type", "") == "text":
            try:
                return json.loads(c.text) or {}
            except (json.JSONDecodeError, TypeError):
                return {"text": c.text}
    return {}


def test_new_chat_reuses_persisted_index(tmp_path):
    """Session A crawls and exits; session B sees indexed_scopes in
    status, resolves '' to the one index, searches, and gets a handle
    fit for get_evidence — no root re-prompt."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    corpus = tmp_path / "corpus"
    _corpus(corpus)
    env = _env(tmp_path / "shared", tmp_path / "workdir",
               tmp_path / "daemon")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "local_knowledge.server_local"], env=env)

    async def crawl_and_exit():
        async with stdio_client(params) as (r, w), \
                ClientSession(r, w) as s:
            await s.initialize()
            h = (await _call(s, "connect",
                             {"root": str(corpus)}))["scope_handle"]
            job = (await _call(s, "start_crawl",
                               {"scope_handle": h}))["job_id"]
            deadline = time.monotonic() + 120
            while True:
                p = await _call(s, "crawl_step",
                                {"scope_handle": h, "job_id": job})
                if p.get("state") not in (None, "running", "starting"):
                    break
                assert time.monotonic() < deadline, p
                await asyncio.sleep(0.5)
            assert p["state"] == "done", p
            return h

    async def new_chat_reuses(handle: str):
        # a brand-new stdio process — the 'new chat' the agent hit
        async with stdio_client(params) as (r, w), \
                ClientSession(r, w) as s:
            await s.initialize()
            st = await _call(s, "status", {})
            idx = st.get("indexed_scopes") or []
            assert idx and idx[0]["scope_handle"] == handle, st
            # agent's reuse path: no root supplied, one index → resolve
            hits = await _call(
                s, "search",
                {"scope_handle": "", "question": "who owns rollback"})
            assert hits.get("evidence"), hits
            assert hits.get("scope_handle") == handle, hits
            ev = await _call(s, "get_evidence",
                             {"scope_handle": hits["scope_handle"],
                              "chunk_ids": [e["chunk_id"] for e in
                                            hits["evidence"][:1]]})
            assert ev, ev

    handle = asyncio.run(crawl_and_exit())
    asyncio.run(new_chat_reuses(handle))
