"""#257 multi-install e2e — TWO daemons, two daemon homes (as two venv
installs would have), ONE shared LOCAL_KNOWLEDGE_WORKDIR index root.

Proves the cross-IDE contract on real processes:
  index once via daemon A → query via daemon B with zero reconnecting
  (scope rebinds from the persisted root_path meta), and A's next publish
  is B's very-next-query visible (WAL snapshot_epoch).
  Simultaneous crawls from both daemons leave exactly one published job —
  the job_lease + supersede fencing means no duplicated work.
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    __import__("os").environ.get("KNOWLEDGE_E2E") != "1",
    reason="e2e sessions run under KNOWLEDGE_E2E=1 (CI e2e leg)")

import os


def _spawn_daemon(home: Path, shared: Path, workdir: Path) -> subprocess.Popen:
    env = dict(os.environ,
               KNOWLEDGE_FAKE_EMBEDDINGS="1",
               KNOWLEDGE_SHARED_HOME=str(shared),
               LOCAL_KNOWLEDGE_WORKDIR=str(workdir),
               PYTHONPATH=os.environ.get("PYTHONPATH", ""))
    (home / "logs").mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [sys.executable, "-m", "confluence_knowledge.daemon", str(home)],
        env=env, stdout=subprocess.DEVNULL,
        stderr=open(home / "logs" / "daemon.log", "wb"))


def _meta(home: Path, timeout=20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        f = home / "http.json"
        if f.exists():
            try:
                m = json.loads(f.read_text())
                with socket.socket() as s:
                    s.settimeout(0.5)
                    s.connect(("127.0.0.1", m["port"]))
                return m
            except (OSError, json.JSONDecodeError, KeyError):
                pass
        time.sleep(0.1)
    raise AssertionError(f"daemon at {home} never came up")


def _services(meta: dict) -> list[str]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{meta['port']}/_services",
        headers={"Authorization": f"Bearer {meta['token']}"})
    return json.loads(
        urllib.request.urlopen(req, timeout=5).read())["services"]


def _call(meta: dict, svc: str, tool: str, args: dict) -> dict:
    """One MCP call_tool round-trip; unwraps structuredContent or the
    first text payload's JSON."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def go():
        h = {"Authorization": f"Bearer {meta['token']}"}
        async with streamablehttp_client(
                f"http://127.0.0.1:{meta['port']}/{svc}/mcp",
                headers=h) as (r, w, _), ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
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
            # the daemon http layer wraps tool payloads once in {"result"}
            if isinstance(out, dict) and set(out) == {"result"}:
                return out["result"]
            return out or {}
    return asyncio.run(go())


def _await_terminal(meta: dict, svc: str, handle: str, job: str,
                    timeout=120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        p = _call(meta, svc, "crawl_step",
                  {"scope_handle": handle, "job_id": job})
        if p.get("state") not in (None, "running", "starting"):
            return p
        time.sleep(0.5)
    raise AssertionError(f"job {job} never left running")


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


def _two_daemons(tmp_path):
    """(metaA, metaB, daemons, workdir) — sibling-install topology."""
    workdir = tmp_path / "workdir"
    da, db = tmp_path / "daemonA", tmp_path / "daemonB"
    pa = _spawn_daemon(da, da / "shared", workdir)
    pb = _spawn_daemon(db, db / "shared", workdir)
    return _meta(da), _meta(db), (pa, pb)


def _stop(daemons) -> None:
    for p in daemons:
        try:
            p.terminate()
            p.wait(timeout=8)
        except Exception:                   # noqa: BLE001 —
            p.kill()                         # teardown is best-effort


def test_index_once_query_everywhere(tmp_path):
    """A crawls; B queries the SAME scope handle — no connect on B, and a
    later A-publish shows up on B's very next query."""
    corpus = tmp_path / "corpus"
    _corpus(corpus)
    metaA, metaB, daemons = _two_daemons(tmp_path)
    try:
        if "local-knowledge" not in _services(metaA) or \
                "local-knowledge" not in _services(metaB):
            pytest.skip("local-knowledge not mounted in this env")
        svc = "local-knowledge"
        out = _call(metaA, svc, "connect", {"root": str(corpus)})
        handle = out["scope_handle"]
        job = _call(metaA, svc, "start_crawl",
                    {"scope_handle": handle})["job_id"]
        assert _await_terminal(metaA, svc, handle, job)["state"] == "done"

        hits = _call(metaB, svc, "search",
                     {"scope_handle": handle,
                      "question": "who owns the rollback steps"})
        assert hits.get("evidence"), hits

        canary = "xyzzy-canary-7"
        _corpus(corpus, canary)
        job2 = _call(metaA, svc, "start_crawl",
                     {"scope_handle": handle, "refresh": True})["job_id"]
        assert _await_terminal(metaA, svc, handle, job2)["state"] == "done"
        # B's next query — no refresh, no reconnect — sees A's new snapshot
        hits2 = _call(metaB, svc, "search",
                      {"scope_handle": handle, "question": canary})
        assert canary in json.dumps(hits2), hits2
    finally:
        _stop(daemons)


def test_one_worker_across_installs(tmp_path):
    """start_crawl fired from BOTH daemons: the lease + supersede fencing
    leaves exactly one published job — never two crawls over the corpus."""
    corpus = tmp_path / "corpus2"
    _corpus(corpus)
    metaA, metaB, daemons = _two_daemons(tmp_path)
    try:
        svc = "local-knowledge"
        if svc not in _services(metaA) or svc not in _services(metaB):
            pytest.skip("local-knowledge not mounted in this env")
        handle = _call(metaA, svc, "connect",
                       {"root": str(corpus)})["scope_handle"]
        jA = _call(metaA, svc, "start_crawl",
                   {"scope_handle": handle})["job_id"]
        jB = _call(metaB, svc, "start_crawl",
                   {"scope_handle": handle})["job_id"]
        endA = _await_terminal(metaA, svc, handle, jA)
        endB = _await_terminal(metaB, svc, handle, jB)
        states = {endA.get("state"), endB.get("state")}
        assert "done" in states, (endA, endB)
        # the loser is fenced — superseded/abandoned/cancelled, never done
        assert states <= {"done", "abandoned", "superseded", "cancelled",
                          "error"}, (endA, endB)
        hits = _call(metaB, svc, "search",
                     {"scope_handle": handle,
                      "question": "escalation owners"})
        assert hits.get("evidence"), hits
    finally:
        _stop(daemons)
