"""#206 multi-service session — one real daemon mounts two services,
survives SIGKILL via the watchdog, and keeps both tool surfaces live.
The 'any install order' guarantee, exercised on a real process."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KNOWLEDGE_E2E") != "1",
    reason="e2e sessions run under KNOWLEDGE_E2E=1 (CI e2e leg)")


def _spawn_daemon(home: Path, shared: Path) -> subprocess.Popen:
    env = dict(os.environ,
               KNOWLEDGE_FAKE_EMBEDDINGS="1",
               KNOWLEDGE_SHARED_HOME=str(shared),
               PYTHONPATH=os.environ.get("PYTHONPATH", ""))
    return subprocess.Popen(
        [sys.executable, "-m", "confluence_knowledge.daemon", str(home)],
        env=env, stdout=subprocess.DEVNULL,
        stderr=open(home / "daemon.log", "wb"))


def _meta(home: Path, timeout=15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        f = home / "http.json"
        if f.exists():
            m = json.loads(f.read_text())
            try:
                with socket.socket() as s:
                    s.settimeout(0.5)
                    s.connect(("127.0.0.1", m["port"]))
                return m
            except OSError:
                pass
        time.sleep(0.1)
    raise AssertionError("daemon http endpoint never came up")


def _services(meta: dict) -> list[str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{meta['port']}/_services",
        headers={"Authorization": f"Bearer {meta['token']}"})
    return json.loads(urllib.request.urlopen(req, timeout=5).read())["services"]


def _tools(meta: dict, svc: str) -> list[str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def go():
        h = {"Authorization": f"Bearer {meta['token']}"}
        async with streamablehttp_client(
                f"http://127.0.0.1:{meta['port']}/{svc}/mcp",
                headers=h) as (r, w, _), ClientSession(r, w) as s:
            await s.initialize()
            return sorted(t.name for t in (await s.list_tools()).tools)
    return asyncio.run(go())


def _spawn_watchdog(home: Path, shared: Path) -> subprocess.Popen:
    env = dict(os.environ, KNOWLEDGE_SHARED_HOME=str(shared),
               PYTHONPATH=os.environ.get("PYTHONPATH", ""))
    return subprocess.Popen(
        [sys.executable, "-m", "confluence_knowledge.watchdog", str(home)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_one_daemon_two_services_survive_kill(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setenv("KNOWLEDGE_SHARED_HOME", str(shared))
    from confluence_knowledge.settings import daemon_home
    home = daemon_home()          # the resolved venv slot — the watchdog
    home.mkdir(parents=True)      # respawns HERE, not a custom dir
    daemon = _spawn_daemon(home, shared)
    watchdog = _spawn_watchdog(home, shared)
    try:
        meta = _meta(home)
        # assert the daemon mounted exactly what's importable in this
        # env — CI legs vary in which wheels they install; the contract
        # is "all installed services, any subset", not a fixed three
        from confluence_knowledge.daemon import discover_services
        expected = discover_services()
        svcs = _services(meta)
        assert sorted(svcs) == sorted(expected), (svcs, expected)
        assert "confluence-knowledge" in svcs   # this leg always has it
        t1 = _tools(meta, "confluence-knowledge")
        assert "search" in t1
        t2 = None
        if "local-knowledge" in svcs:
            t2 = _tools(meta, "local-knowledge")
            assert "connect" in t2

        # hard kill — the watchdog (not the IDE) must bring it back
        daemon.kill()
        daemon.wait(timeout=5)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                meta = _meta(home, timeout=3)
                if set(_services(meta)) >= set(svcs):
                    break
            except Exception:  # noqa: BLE001,S110 — endpoint not
                pass                   # respawned yet — keep polling
            time.sleep(0.5)
        else:
            pytest.fail("watchdog never respawned the daemon")
        # every mounted surface still answers post-respawn
        assert "search" in _tools(meta, "confluence-knowledge")
        if t2 is not None:
            assert "connect" in _tools(meta, "local-knowledge")
    finally:
        (home / ".watchdog.stop").write_text("1")
        watchdog.terminate()
        try:
            watchdog.wait(timeout=8)
        except subprocess.TimeoutExpired:
            watchdog.kill()
        try:
            import psutil
            for p in psutil.process_iter(["pid", "cmdline"]):
                if p.info["cmdline"] and \
                        "confluence_knowledge.daemon" in " ".join(
                            p.info["cmdline"]):
                    p.terminate()
        except Exception:  # noqa: BLE001,S110 — teardown is
            pass                       # best-effort
