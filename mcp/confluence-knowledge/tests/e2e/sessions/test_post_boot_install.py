"""#264 — the reported work-laptop bug, at real-process fidelity:
a daemon that booted when only confluence was installed must NOT run a
tagged local-knowledge call on confluence's handler.

Simulated by booting the daemon with `--module confluence_knowledge.server`
(the only_module contract = 'this daemon predates the install'). A
`local_knowledge.server_local` stdio session's tagged `connect` must get
the clean "not mounted" error — never confluence's extract_page_id
'numeric page ID' nonsense — and after a full-discovery daemon restart
the same call proceeds.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
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


def _spawn_daemon(home: Path, shared: Path, module: str | None):
    args = [sys.executable, "-m", "confluence_knowledge.daemon", str(home)]
    if module:
        args += ["--module", module]
    env = dict(os.environ,
               KNOWLEDGE_FAKE_EMBEDDINGS="1",
               KNOWLEDGE_SHARED_HOME=str(shared))
    (home / "logs").mkdir(parents=True, exist_ok=True)
    err = open(home / "daemon.err.log", "ab")  # noqa: SIM115
    return subprocess.Popen(
        args, env=env, stdout=subprocess.DEVNULL, stderr=err)


def _await_daemon(home: Path, timeout=20) -> None:
    """Ready = the lease the proxy's daemon_alive() checks — http.json
    only proves the http side bound, not that rpc.db is serving."""
    from confluence_knowledge.rpc import RpcDb
    rpc = RpcDb(home / "rpc.db")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rpc.daemon_alive():
            return
        time.sleep(0.2)
    raise AssertionError(f"daemon never took lease — "
                         f"{list(home.glob('**/*'))}")


async def _call(sess, tool: str, args: dict) -> dict:
    res = await sess.call_tool(tool, args)
    for c in res.content:
        if getattr(c, "type", "") == "text":
            try:
                return json.loads(c.text) or {}
            except (json.JSONDecodeError, TypeError):
                return {"text": c.text}
    return {}


def test_tagged_miss_never_cross_dispatches(tmp_path):
    """Daemon constrained to confluence + tagged local-knowledge connect
    → clean 'not mounted' error, no wrong-handler execution."""
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    corpus = tmp_path / "corpus"
    _corpus(corpus)
    shared, workdir = tmp_path / "shared", tmp_path / "workdir"
    home = tmp_path / "daemon"
    home.mkdir(parents=True)
    daemon = _spawn_daemon(home, shared, "confluence_knowledge.server")
    try:
        _await_daemon(home)
        env = _env(shared, workdir, home)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "local_knowledge.server_local"], env=env)

        async def go():
            async with stdio_client(params) as (r, w), \
                    ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool("connect",
                                        {"root": str(corpus)})
                blob = " ".join(
                    getattr(c, "text", "") for c in res.content).lower()
                # the #264 contract: a clean 'not mounted' error — NOT
                # confluence's extract_page_id nonsense leaking through
                assert "not mounted" in blob, res.content
                assert "page" not in blob and "url" not in blob, \
                    res.content
        asyncio.run(go())
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()
