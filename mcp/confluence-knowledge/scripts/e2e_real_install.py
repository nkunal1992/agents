"""Real-install E2E — the zero-reload flagship test (#271).

A REAL venv, REAL wheels, REAL pip install mid-flight — the scenario the
unit tests can only fake:

    build wheels → fresh venv → pip install confluence-knowledge
    → spawn daemon → unified stdio session (assert only confluence__*)
    → pip install local-knowledge INTO THE LIVE VENV
    → tools/list grows local__* with ZERO daemon/proxy restarts
    → local__connect + crawl + search through the namespaced union
    → pip uninstall local-knowledge → tools vanish next list
    → register writes the `knowledge` entry by default (#269)

Nothing here is stubbed except the embedder (KNOWLEDGE_FAKE_EMBEDDINGS=1
keeps it under a minute without the 1.3 GB model pull). Run from the
repo checkout:

    python scripts/e2e_real_install.py
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _e2e_common import check, fresh_venv, stop_shared_daemon, venv_bin
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PKGS = Path(__file__).resolve().parents[2]
CONF = PKGS / "confluence-knowledge"
LOCAL = PKGS / "local-knowledge"


def _run(argv, env, msg, check_rc=True, timeout=600):
    p = subprocess.run(argv, check=False, env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    check(not check_rc or p.returncode == 0,
          msg, f"rc={p.returncode}\n{p.stdout[-800:]}\n{p.stderr[-800:]}")
    return p


def _env(sandbox: Path) -> dict:
    e = dict(os.environ,
             HOME=str(sandbox / "home"),
             USERPROFILE=str(sandbox / "home"),
             KNOWLEDGE_FAKE_EMBEDDINGS="1",
             KNOWLEDGE_SHARED_HOME=str(sandbox / "shared"),
             KNOWLEDGE_DAEMON_HOME=str(sandbox / "daemon"),
             # exercises the daemon lifecycle — opt in (local-first is
             # the default since #285)
             KNOWLEDGE_DAEMON="1",
             CONF_KNOWLEDGE_WORKDIR=str(sandbox / "conf-wd"),
             LOCAL_KNOWLEDGE_WORKDIR=str(sandbox / "local-wd"),
             PIP_DISABLE_PIP_VERSION_CHECK="1")
    e.pop("PYTHONPATH", None)
    return e


def _corpus(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "runbook.md").write_text(
        "# Deployment runbook\n\nThe oncall runbook covers rollback "
        "steps, escalation owners, and the production cutover "
        "checklist.\n" * 20)


async def _call(sess, tool: str, args: dict) -> dict:
    res = await sess.call_tool(tool, args)
    out = getattr(res, "structuredContent", None)
    if isinstance(out, dict) and set(out) == {"result"}:
        return out["result"]
    if out:
        return out
    for c in res.content:
        if getattr(c, "type", "") == "text":
            try:
                out = json.loads(c.text)
            except (json.JSONDecodeError, TypeError):
                return {"text": c.text}
            if isinstance(out, dict) and set(out) == {"result"}:
                return out["result"]
            return out
    return {}


async def _names(sess) -> list[str]:
    return sorted(t.name for t in (await sess.list_tools()).tools)


def _failed(e: BaseException) -> bool:
    """check() raises SystemExit inside the session's anyio task group —
    asyncio wraps it in a BaseExceptionGroup. Detect our own failure
    marker anywhere in the tree so we can exit quietly (the FAIL line
    was already printed) instead of dumping a teardown traceback."""
    if isinstance(e, SystemExit):
        return True
    if isinstance(e, BaseExceptionGroup):
        return any(_failed(x) for x in e.exceptions)
    return False


def _teardown_noise(e: BaseException) -> bool:
    """The proxy child exits on stdin EOF while the client's reader task
    still holds a buffered message — mcp's stdio_client surfaces that as
    a BrokenResourceError group at __aexit__. Harmless once the checks
    inside the session body have all run; tolerated iff EVERY leaf is
    stream-lifecycle noise."""
    import anyio
    noise = (anyio.BrokenResourceError, anyio.ClosedResourceError,
             anyio.EndOfStream, BrokenPipeError, ConnectionResetError)
    if isinstance(e, noise):
        return True
    if isinstance(e, BaseExceptionGroup):
        return all(_teardown_noise(x) for x in e.exceptions)
    return False


def _await_daemon(home: Path, timeout=30) -> None:
    meta = home / "http.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if meta.exists():
            return
        time.sleep(0.3)
    check(False, "daemon never wrote http.json",
          list(home.glob("**/*")))


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="kreal-") as td:
        sandbox = Path(td)
        env = _env(sandbox)
        (sandbox / "home").mkdir(parents=True)
        wheels = sandbox / "wheels"
        wheels.mkdir()

        # 1. build wheels from THIS checkout — unreleased code is fair game
        for pkg in (CONF, LOCAL):
            _run([sys.executable, "-m", "pip", "wheel", "--no-deps",
                  "-w", str(wheels), str(pkg)], env,
                 f"built {pkg.name}")

        # 2. fresh venv + install the confluence wheel only
        venv = sandbox / "venv"
        fresh_venv(venv)
        py = str(venv_bin(venv) / "python")
        conf_whl = next(wheels.glob("confluence_knowledge-*.whl"))
        _run([py, "-m", "pip", "install", str(conf_whl)], env,
             "pip install confluence-knowledge wheel", timeout=900)

        # 3. daemon up — discovered services = confluence only. The
        # console `stop` runs at ANY exit as a backstop so a failure can
        # never orphan the daemon+watchdog pair into a respawn pile-up
        cli = str(venv_bin(venv) / "confluence-knowledge")
        atexit.register(stop_shared_daemon, cli, env)
        dhome = sandbox / "daemon"
        dhome.mkdir()
        derr = open(sandbox / "daemon.stderr.log", "wb")  # noqa: SIM115
        daemon = subprocess.Popen(
            [py, "-m", "confluence_knowledge.daemon", str(dhome)],
            env=env, stdout=subprocess.DEVNULL, stderr=derr)
        try:
            _await_daemon(dhome)
            check(True, "daemon up")

            params = StdioServerParameters(
                command=py,
                args=["-m", "confluence_knowledge.unified"], env=env)

            session_done = False   # body finished every check

            async def go():
                nonlocal session_done
                async with stdio_client(params) as (r, w), \
                        ClientSession(r, w) as s:
                    await s.initialize()
                    names = await _names(s)
                    check(names and all("__" in n for n in names),
                          "unified list is namespaced", names)
                    check(any(n.startswith("confluence__") for n in names)
                          and not any(n.startswith("local__")
                                      for n in names),
                          "only confluence__* before install", names)
                    st = await _call(s, "confluence__status", {})
                    check(isinstance(st, dict) and st,
                          "confluence__status routed", st)

                    # 4. LIVE install — the flagship moment
                    local_whl = next(
                        wheels.glob("local_knowledge-*.whl"))
                    _run([py, "-m", "pip", "install", "--no-deps",
                          str(local_whl)], env,
                         "pip install local-knowledge into LIVE venv",
                         timeout=600)

                    names2 = await _names(s)   # same session, zero restarts
                    check(any(n.startswith("local__") for n in names2),
                          "local__* appeared without any restart",
                          names2)

                    corpus = sandbox / "corpus"
                    _corpus(corpus)
                    # the daemon's first tagged call to the new service
                    # hot-mounts via remount — a heavy module import that
                    # can trip rpc.wait's stale check once; the "retry in
                    # a few seconds" contract is explicitly retryable
                    conn, h = {}, None
                    for _ in range(20):
                        conn = await _call(s, "local__connect",
                                           {"root": str(corpus)})
                        h = conn.get("scope_handle")
                        if h and h.startswith("scope:"):
                            break
                        check(conn.get("daemon") == "starting",
                              "unexpected connect error", conn)
                        await asyncio.sleep(1)
                    check(h and h.startswith("scope:"),
                          "local__connect routed through live union",
                          conn)
                    job = (await _call(s, "local__start_crawl",
                                       {"scope_handle": h}))["job_id"]
                    deadline = time.monotonic() + 120
                    while True:
                        p = await _call(s, "local__crawl_step",
                                        {"scope_handle": h,
                                         "job_id": job})
                        if p.get("state") not in (None, "running",
                                                  "starting"):
                            break
                        check(time.monotonic() < deadline,
                              "crawl finished", p)
                        await asyncio.sleep(0.5)
                    check(p["state"] == "done", "crawl done", p)
                    hits = await _call(s, "local__search",
                                       {"scope_handle": h,
                                        "question": "who owns rollback"})
                    check(bool(hits.get("evidence")),
                          "search through union returns evidence", hits)

                    # 5. LIVE uninstall — union shrinks
                    _run([py, "-m", "pip", "uninstall", "-y",
                          "local-knowledge"], env,
                         "pip uninstall local-knowledge")
                    names3 = await _names(s)
                    check(not any(n.startswith("local__") for n in names3)
                          and any(n.startswith("confluence__")
                                  for n in names3),
                          "local__* vanished on next list", names3)
                    session_done = True
            try:
                asyncio.run(go())
            except BaseException as e:
                if _failed(e):
                    return 1   # check() already printed the FAIL line
                # noise mid-session = dead proxy = a real failure, not noise
                if session_done and _teardown_noise(e):
                    print("(stdio teardown noise — all checks passed)")
                else:
                    raise

            # 6. register defaults to the unified `knowledge` entry (#269)
            _run([cli, "register"], env, "register (default)")
            hits = list((sandbox / "home").rglob("mcp.json"))
            found = False
            for p in hits:
                try:
                    doc = json.loads(p.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                servers = doc.get("mcpServers", doc.get("servers", {}))
                if "knowledge" in servers:
                    found = True
            check(found, "register wrote the `knowledge` entry",
                  [str(p) for p in hits])
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()
            tail = (sandbox / "daemon.stderr.log")
            if tail.exists():
                print("---- daemon stderr (last 30) ----")
                print("\n".join(tail.read_text(
                    errors="replace").splitlines()[-30:]))
    print("PASS real-install e2e")
    return 0


if __name__ == "__main__":
    sys.exit(main())
