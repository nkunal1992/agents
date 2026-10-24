"""#206 — the daemon-hosted HTTP endpoint is the reconnectable transport.

The field failure: a stdio proxy dies mid-request (sleep/timeout) and only
an IDE reload recovers it. Over HTTP there's no child to kill — the client
re-POSTs per request. These sessions exercise the real MCP streamable-http
protocol against a live daemon: connect → tools → daemon kill → doctor
self-heal → same URL answers → SIGSTOP/SIGCONT (the sleep simulation).
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

pytestmark = pytest.mark.skipif(
    os.environ.get("KNOWLEDGE_E2E") != "1",
    reason="e2e sessions run under KNOWLEDGE_E2E=1 (CI e2e leg)")


def _spawn_daemon(workdir: Path) -> subprocess.Popen:
    env = dict(os.environ, KNOWLEDGE_FAKE_EMBEDDINGS="1")
    return subprocess.Popen(
        [sys.executable, "-m", "confluence_knowledge.daemon",
         str(workdir)],
        env=env, stdout=subprocess.DEVNULL,
        stderr=open(workdir / "daemon.log", "ab"))


def _wait_http(workdir: Path, timeout: float = 12) -> dict:
    """Wait for the endpoint to actually accept connections — http.json is
    written before uvicorn finishes binding, so the file alone races."""
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        f = workdir / "http.json"
        if f.exists():
            meta = json.loads(f.read_text())
            try:
                with socket.socket() as probe:
                    probe.settimeout(0.5)
                    probe.connect(("127.0.0.1", meta["port"]))
                return meta
            except OSError:
                pass
        time.sleep(0.1)
    raise AssertionError("daemon http endpoint never came up")


async def _call(url: str, token: str, tool: str, args: dict) -> dict:
    """One MCP session — reconnect-per-request is the point, so each call
    opens a fresh streamable-http session like a fresh IDE request."""
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (r, w, _), ClientSession(r, w) as session:
        await session.initialize()
        res = await session.call_tool(tool, args)
        text = "".join(c.text for c in res.content
                       if getattr(c, "type", "") == "text")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}


def _url(meta: dict, svc: str = "confluence-knowledge") -> str:
    return f"http://127.0.0.1:{meta['port']}/{svc}/mcp"


@pytest.fixture
def daemon(workdir):
    proc = _spawn_daemon(workdir)
    yield proc
    (workdir / ".watchdog.stop").write_text("1")   # stop the respawn loop
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    try:                            # a watchdog-respawned daemon may be
        import psutil  # running under a different pid
        for p in psutil.process_iter(["cmdline"]):
            if p.info["cmdline"] and "confluence_knowledge.daemon" \
                    in " ".join(p.info["cmdline"]) and \
                    str(workdir) in " ".join(p.info["cmdline"]):
                p.terminate()
    except Exception:               # noqa: BLE001,S110 — teardown is
        pass                         # best-effort


@pytest.fixture
def workdir(tmp_path):
    # the watchdog respawns at daemon_home(), not an arbitrary dir — the
    # fixture must live there for the self-eject → respawn cycle to be
    # observable by the client (same port via the persisted http.json)
    from confluence_knowledge.settings import daemon_home
    d = daemon_home()
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_http_endpoint_answers_tools(daemon, workdir):
    meta = _wait_http(workdir)
    out = asyncio.run(_call(_url(meta), meta["token"], "status", {}))
    assert isinstance(out, dict)
    # unauthorized is refused — loopback is a multi-user surface
    async def unauth():
        try:
            async with streamablehttp_client(
                    _url(meta), headers={"Authorization": "Bearer x"}
            ) as (r, w, _), ClientSession(r, w) as s:
                await s.initialize()
                return "connected"
        except Exception:  # noqa: BLE001 — session asserts the failure shape
            return "rejected"
    assert asyncio.run(unauth()) == "rejected"


def test_daemon_restart_same_url(daemon, workdir):
    meta = _wait_http(workdir)
    asyncio.run(_call(_url(meta), meta["token"], "status", {}))
    # hard kill — the field's worst case. Under watchdog supervision
    # "the URL stays dead" is impossible BY DESIGN: the watchdog respawns
    # the daemon, which re-binds the persisted port. The contract the
    # field needs is recovery-on-same-URL, so assert exactly that.
    daemon.kill()
    daemon.wait(timeout=8)
    # 90s on CI: watchdog detect + cold spawn + fastmcp/uvicorn import +
    # mount can stretch past 60 on an AV-scanned Windows runner — the
    # contract is 'respawn happens on the same URL', not 'in 60s'
    deadline = time.monotonic() + 90
    while True:
        try:
            out = asyncio.run(asyncio.wait_for(
                _call(_url(meta), meta["token"], "status", {}), 5))
            break                                  # a respawn answered
        except Exception:                          # noqa: BLE001 — gap
            if time.monotonic() > deadline:
                raise AssertionError(
                    "watchdog never respawned the daemon") from None
            time.sleep(0.5)
    # same URL + token: the registered IDE entry never changes
    meta2 = json.loads((workdir / "http.json").read_text())
    assert meta2["port"] == meta["port"], \
        "daemon restart must re-bind the registered port"
    assert meta2["token"] == meta["token"]
    assert isinstance(out, dict) and "configured" in out


def test_jetbrains_query_token_auth(daemon, workdir):
    """JetBrains Copilot drops mcp.json headers entirely (upstream
    microsoft/copilot-intellij-feedback#1579) — its real request shape
    is NO headers + ?token= in the URL. This replays that shape exactly:
    zero headers, token in the query string."""
    meta = _wait_http(workdir)
    url = f"http://127.0.0.1:{meta['port']}/confluence-knowledge/mcp?token={meta['token']}"
    async def noheaders():
        async with streamablehttp_client(url) as (r, w, _), \
                ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool("status", {})
            return json.loads(
                "".join(c.text for c in res.content
                        if getattr(c, "type", "") == "text"))
    out = asyncio.run(noheaders())
    assert isinstance(out, dict) and "configured" in out


def test_suspend_wake_no_reload(daemon, workdir):
    """SIGSTOP suspends the daemon — the laptop-lock simulation. The client
    times out mid-wait; on SIGCONT the next request just answers — no
    respawn, no reload."""
    if not hasattr(signal, "SIGSTOP"):
        pytest.skip("no SIGSTOP on this platform")
    meta = _wait_http(workdir)
    daemon.send_signal(signal.SIGSTOP)
    try:
        with pytest.raises(Exception):  # noqa: B017 — suspended transport
                                      # fails however the client surfaces it
            asyncio.run(asyncio.wait_for(
                _call(_url(meta), meta["token"], "status", {}), 4))
    finally:
        daemon.send_signal(signal.SIGCONT)
    # post-SIGCONT the daemon's session manager can take a beat to re-sync —
    # the contract is "requests answer again, no reload", not "the very
    # first post-wake call is guaranteed". One retry window covers the
    # manager's stale-session GC; a dead daemon would never answer.
    out = None
    for _ in range(5):
        try:
            out = asyncio.run(_call(_url(meta), meta["token"],
                                      "status", {}))
            break
        except Exception:                   # noqa: BLE001 — session
            time.sleep(1)                    # manager still re-syncing
    assert isinstance(out, dict) and "configured" in out
