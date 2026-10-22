#!/usr/bin/env python3
"""User-journey e2e — replays the exact navigation a new user takes, on
the production code path (daemon ON, real server over stdio), against
the fake DC + fake embeddings.

    python scripts/e2e_journey.py [--corpus tests/fixtures/public_corpus.json]

Journey: fresh HOME → status says unconfigured → register_token with a
bad clipboard (agent re-asks) → good clipboard → masked confirm →
connect → crawl → plain-language pulse → search → verify → feedback →
daemon SIGKILL mid-flow → next call respawns it → crawl completes.
No models, no network beyond localhost — runs on every CI leg.
"""

import asyncio
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).parent))

import atexit as _atexit
import subprocess as _sp


def _atexit_stop(env: dict) -> None:
    """Stop the shared daemon/watchdog this run may have spawned — CI
    leaves no orphans (post-checkout kills them and marks the job failed
    on Windows)."""
    _env = dict(env)
    # the script imports via sys.path — a plain interpreter doesn't see
    # the package; put src on PYTHONPATH for the stop call
    _env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[1] / "src")]
        + ([_env["PYTHONPATH"]] if _env.get("PYTHONPATH") else []))
    _atexit.register(lambda: _sp.run(
        [sys.executable, "-m", "confluence_knowledge.cli", "stop"],
        env=_env, capture_output=True, timeout=30, check=False))

from _e2e_common import TOKEN, call, check, serve


def _find_daemon(work: Path) -> int | None:
    """Daemon pid from the rpc lease — portable (Windows has no pgrep)."""
    import sqlite3
    rpc = work / ".conf_knowledge_index" / "rpc.db"
    if not rpc.exists():
        return None
    try:
        row = sqlite3.connect(str(rpc)).execute(
            "SELECT pid FROM daemon_lease").fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


async def main() -> int:
    corpus = None
    for i, a in enumerate(sys.argv):
        if a == "--corpus" and i + 1 < len(sys.argv):
            corpus = sys.argv[i + 1]
    if corpus:
        os.environ["FAKE_DC_CORPUS"] = str(Path(corpus).resolve())

    srv, base, _hits = serve()
    home = Path(tempfile.mkdtemp(prefix="ck-journey-home-"))
    work = Path(tempfile.mkdtemp(prefix="ck-journey-work-"))
    clip = work / "clipboard.txt"                # the OS-clipboard stand-in
    env = {**os.environ, "CONF_KNOWLEDGE_HOME": str(home),
           "CONF_KNOWLEDGE_WORKDIR": str(work),
           "KNOWLEDGE_FAKE_EMBEDDINGS": "1",
           "CONF_KNOWLEDGE_CLIPBOARD_FILE": str(clip),
           "CONF_KNOWLEDGE_STEP_SECONDS": "4"}
    env.pop("CONF_KNOWLEDGE_TOKEN", None)        # register_token must matter
    _atexit_stop(env)

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "confluence_knowledge.server"], env=env,
        cwd=tempfile.gettempdir())

    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = {t.name for t in (await s.list_tools()).tools}
        check({"status", "register_token", "connect", "start_crawl",
               "crawl_step", "wait_for_crawl", "cancel_crawl", "search",
               "get_evidence", "verify_citations", "submit_feedback",
               "feedback_report"} <= tools, f"full tool set: {sorted(tools)}")

        st = await call(s, "status")
        check(not st["configured"], "fresh home → unconfigured")

        # --- onboarding: bad clipboard → re-ask → good clipboard ---
        clip.write_text("not-a-real-token")
        # keyring write + token validation can exceed the default 20 s
        # on CI runners (Windows Credential Manager especially)
        bad = await call(s, "register_token",
                         {"site": base, "env": "datacenter"}, timeout=90)
        check(bad.get("configured") is not True
              or "error" in bad or "_error" in bad,
              "register_token rejects a bad clipboard", bad)

        clip.write_text(TOKEN)
        good = await call(s, "register_token",
                          {"site": base, "env": "datacenter"}, timeout=90)
        check(good.get("configured") is True
              and "…" in str(good.get("token_received", ""))
              and TOKEN not in str(good),
              "register_token: masked confirm, credential stored, "
              "token never echoed", good)

        # --- connect + crawl ---
        c = await call(s, "connect", {"root": f"{base}/pages/viewpage.action?pageId=100"})
        scope = c["scope_handle"]
        job = await call(s, "start_crawl", {"scope_handle": scope})

        # --- daemon kill mid-crawl → respawn + resume, user sees nothing ---
        deadline = time.monotonic() + 8
        dpid = None
        while time.monotonic() < deadline:
            dpid = _find_daemon(work)
            if dpid:
                break
            await asyncio.sleep(0.3)
        if dpid:
            os.kill(dpid, getattr(signal, 'SIGKILL', signal.SIGTERM))
            check(True, f"daemon killed mid-crawl (pid {dpid})")

        final, waited = {"state": "running"}, 0.0
        while final["state"] not in ("done", "abandoned", "cancelled"):
            check(waited <= 180, "crawl finishes within 180s", final)
            await asyncio.sleep(1.0)
            waited += 1.0
            final = await call(s, "crawl_step",
                               {"scope_handle": scope,
                                "job_id": job["job_id"]})
            pl = final.get("pulse", "")
            if pl:
                check(" ppm" not in pl and "RSS" not in pl,
                      "pulse stays plain-language — no ppm/RSS", pl)
        check(final["state"] == "done", "crawl completes post-kill", final)

        # --- the RAG tail ---
        res = await call(s, "search", {"scope_handle": scope,
                                       "question": "deploy windows"})
        check(bool(res.get("evidence")), "search returns evidence", res)
        if res.get("evidence"):
            ok = await call(s, "verify_citations",
                            {"scope_handle": scope,
                             "evidence_set": res["evidence_set"],
                             "mappings": [{
                                 "claim": "c",
                                 "chunk_id": res["evidence"][0]["chunk_id"],
                                 "quote": res["evidence"][0]["text"][:40]}]})
            check(ok.get("ok") is True, "verify_citations round-trips "
                  "through the daemon", ok)
        fb = await call(s, "submit_feedback",
                        {"scope_handle": scope, "question": "q",
                         "correction": "c", "note": "journey"})
        check(isinstance(fb, dict), "submit_feedback answers", fb)
        rep = await call(s, "feedback_report", {"scope_handle": scope})
        check(isinstance(rep, dict), "feedback_report answers", rep)

        st = await call(s, "status")
        check(st["configured"], "status: configured at journey end", st)

    srv.shutdown()
    print("\njourney: all checks passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
