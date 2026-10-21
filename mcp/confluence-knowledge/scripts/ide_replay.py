#!/usr/bin/env python3
"""FakeIDE — a scripted replay of an IDE's MCP session. The wire protocol
is the IDE's real surface (initialize → tools/list → call → notifications
→ spawn/kill semantics); the LLM's *choices* inside it are scripted, not
simulated — every scenario is a field-observed or designed navigation.

Session scripts live in sessions/*.json:

  {"name": "fresh-onboarding",
   "env": {"KNOWLEDGE_FAKE_EMBEDDINGS": "1"},
   "steps": [
     {"verb": "spawn_server"},
     {"verb": "tools", "must_contain": ["status", "connect"]},
     {"verb": "call", "tool": "status", "expect": {"configured": false}},
     {"verb": "call", "tool": "register_token",
      "args": {"site": "{base}", "env": "datacenter"},
      "expect": {"configured": true}},
     {"verb": "call", "tool": "connect",
      "args": {"root": "{base}/pages/viewpage.action?pageId=100"},
      "save": {"scope": "scope_handle"}},
     {"verb": "call", "tool": "start_crawl",
      "args": {"scope_handle": "{scope}"}, "save": {"job": "job_id"}},
     {"verb": "wait_crawl", "scope": "{scope}", "job": "{job}",
      "timeout_s": 180},
     {"verb": "kill_daemon"},
     {"verb": "call", "tool": "search",
      "args": {"scope_handle": "{scope}", "question": "deploy windows"},
      "expect_len": {"evidence": 1}},
     {"verb": "transcript"}]}

Verbs: spawn_server | kill_server | new_session | second_window |
call | wait_crawl | kill_daemon | corrupt_clipboard | set_clipboard |
set_env | sleep | tools | transcript | connect_and_crawl (shortcut)

Run:  python scripts/ide_replay.py sessions/fresh-onboarding.json
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).parent))
from _e2e_common import TOKEN, check, serve


def _daemon_pid(work: Path) -> int | None:
    """Daemon pid from the rpc lease — portable (no pgrep; Windows has
    neither pgrep nor SIGKILL)."""
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


def _sigkill() -> int:
    return getattr(signal, "SIGKILL", signal.SIGTERM)


class FakeIDE:
    """One IDE instance — owns a stdio MCP server + session lifecycle."""

    def __init__(self, home: Path, work: Path, env_extra: dict):
        self.home = home
        self.work = work
        self.env = {**os.environ,
                    "CONF_KNOWLEDGE_HOME": str(home),
                    "CONF_KNOWLEDGE_WORKDIR": str(work),
                    **env_extra}
        self.stack = None
        self.session = None
        self.vars: dict[str, str] = {}
        self.transcript: list[dict] = []
        self._last_evidence: dict | None = None

    async def spawn_server(self):
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "confluence_knowledge.server"],
            env=self.env, cwd=tempfile.gettempdir())
        self.stack = stdio_client(params)
        r, w = await self.stack.__aenter__()
        self.session = ClientSession(r, w)
        await self.session.__aenter__()
        await self.session.initialize()

    async def kill_server(self):
        if self.session is not None:
            # session first — its read pump lives inside the stdio stack's
            # task group; exiting the stack while it runs breaks the
            # anyio cancel scope
            try:
                await self.session.__aexit__(None, None, None)
            finally:
                self.session = None
        if self.stack is not None:
            await self.stack.__aexit__(None, None, None)
            self.stack = None

    async def call(self, tool, args=None, timeout=60, tolerate=False):
        args = {k: self._sub(v) for k, v in (args or {}).items()}
        # citation sugar — sessions can't hand-write chunk ids; pull a
        # real chunk + quote from a saved evidence set
        for m in args.get("mappings") or []:
            if m.get("chunk_id") == "__evidence_first__":
                ev = (self._last_evidence or {}).get(
                    args.get("evidence_set", "")) or []
                m["chunk_id"] = ev[0]["chunk_id"]
                if not m.get("quote"):
                    m["quote"] = ev[0]["text"][:60]
        from datetime import timedelta
        t = time.monotonic()
        r = await self.session.call_tool(
            tool, args, read_timeout_seconds=timedelta(seconds=timeout))
        dt = time.monotonic() - t
        text = r.content[0].text if r.content else ""
        if r.isError and not tolerate:
            raise AssertionError(f"{tool} errored after {dt:.1f}s: {text}")
        out = json.loads(text) if text else {"error": text}
        self.transcript.append({"tool": tool, "args": args,
                                "dt": round(dt, 2), "result": out})
        if out.get("evidence_set") and out.get("evidence"):
            if self._last_evidence is None:
                self._last_evidence = {}
            self._last_evidence[out["evidence_set"]] = out["evidence"]
        return out

    async def wait_crawl(self, scope, job, timeout_s=180,
                         terminal=None, tolerate_errors=False,
                         expect_phase=None):
        terminal = terminal or ("done", "abandoned", "cancelled")
        final, waited = {"state": "running"}, 0.0
        saw_phase = set()
        while final["state"] not in terminal:
            check(waited <= timeout_s,
                  f"crawl finishes within {timeout_s}s", final)
            await asyncio.sleep(1.0)
            waited += 1.0
            final = await self.call(
                "crawl_step", {"scope_handle": scope, "job_id": job})
            body = json.dumps(final, ensure_ascii=False)
            for frag in (expect_phase or []):
                if frag in body:
                    saw_phase.add(frag)
            pl = final.get("pulse", "")
            if pl:
                check(" ppm" not in pl and "RSS" not in pl
                      and "eta_s" not in pl,
                      "pulse stays plain-language", pl)
        for frag in (expect_phase or []):
            check(frag in saw_phase,
                  f"progress surfaced '{frag}' during the wait",
                  saw_phase)
        if not tolerate_errors:
            check(not final.get("errors"),
                  "crawl completed without errors",
                  (final.get("errors") or [])[:3])
        return final

    def _sub(self, v):
        if isinstance(v, str):
            for k, val in self.vars.items():
                v = v.replace("{" + k + "}", str(val))
        return v

    async def run_step(self, step: dict):
        v = step["verb"]
        if v == "spawn_server":
            await self.spawn_server()
        elif v == "kill_server":
            await self.kill_server()
        elif v == "new_session":
            await self.kill_server()
            await self.spawn_server()
        elif v == "call":
            out = await self.call(step["tool"], step.get("args"),
                                  timeout=step.get("timeout", 60),
                                  tolerate=bool(step.get("tolerate")))
            if "expect" in step:
                for key, want in step["expect"].items():
                    check(out.get(key) == want,
                          f"{step['tool']}.{key} == {want}", out)
            if "expect_len" in step:
                for key, n in step["expect_len"].items():
                    check(len(out.get(key) or []) >= n,
                          f"{step['tool']}.{key} len >= {n}", out)
            if "expect_text" in step:
                body = json.dumps(out, ensure_ascii=False)
                for frag in step["expect_text"]:
                    check(frag in body,
                          f"{step['tool']} output contains '{frag}'", out)
            for var, field in (step.get("save") or {}).items():
                self.vars[var] = out[field]
        elif v == "wait_crawl":
            await self.wait_crawl(self._sub(step["scope"]),
                                  self._sub(step["job"]),
                                  step.get("timeout_s", 180),
                                  step.get("terminal"),
                                  step.get("tolerate_errors", False),
                                  step.get("expect_phase"))
        elif v == "kill_daemon":
            pid = _daemon_pid(self.work)
            check(pid is not None, "daemon lease exists to kill")
            os.kill(pid, _sigkill())
            check(True, f"daemon killed (pid {pid})")
            await asyncio.sleep(2)
        elif v == "set_clipboard":
            clip = Path(self.env["CONF_KNOWLEDGE_CLIPBOARD_FILE"])
            clip.write_text(self._sub(step.get("text", "")))
        elif v == "corrupt_clipboard":
            self.vars["bad_clip"] = "not-a-real-token"
            clip = Path(self.env["CONF_KNOWLEDGE_CLIPBOARD_FILE"])
            clip.write_text(self.vars["bad_clip"])
        elif v == "confluence_down":
            self._server.shutdown()
            self._server.server_close()   # release the port — shutdown()
            check(True, "confluence down")  # alone leaves it bound
        elif v == "confluence_up":
            self._server, _, _ = self._serve(int(self.vars["port"]))
            check(True, "confluence back up")
        elif v == "corrupt_index":
            idx = self.work / ".conf_knowledge_index"
            dbs = [d for d in idx.glob("*.db")
                   if "rpc" not in d.name and "feedback" not in d.name]
            check(bool(dbs), "a scope db exists to corrupt", list(idx.glob("*")))
            # WAL keeps live pages out of the main file — trashing .db
            # alone still reads fine. Real damage = corrupt all three.
            for ext in ("", "-wal", "-shm"):
                f = Path(str(dbs[0]) + ext)
                if f.exists():
                    f.write_bytes(b"garbage" * 2000)
            check(True, f"index corrupted: {dbs[0].name} (+wal/shm)")
        elif v == "set_env":
            self.env.update(step.get("env", {}))
        elif v == "sleep":
            await asyncio.sleep(step.get("s", 1))
        elif v == "tools":
            tools = {t.name for t in
                     (await self.session.list_tools()).tools}
            want = set(step.get("must_contain", []))
            check(want <= tools, f"tools has {sorted(want)}", sorted(tools))
        elif v == "burst":
            import asyncio as _a
            await _a.gather(*[self.call(step["tool"],
                                        step.get("args"))
                              for _ in range(step["n"])])
            check(True, f"burst {step['tool']}×{step['n']} all clean")
        elif v == "spawn_window2":
            # a second IDE window — fresh server process, same
            # HOME/WORKDIR (the WINDOWS/IDEKILL field topology)
            self.win2 = FakeIDE(self.home, self.work, self.env)
            self.win2.vars = self.vars
            await self.win2.spawn_server()
        elif v == "call2":
            args = {k: self._sub(x)
                    for k, x in (step.get("args") or {}).items()}
            from datetime import timedelta
            r = await self.win2.session.call_tool(
                step["tool"], args,
                read_timeout_seconds=timedelta(
                    seconds=step.get("timeout", 60)))
            text = r.content[0].text if r.content else ""
            out = json.loads(text) if text else {}
            if "expect" in step:
                for key, want in step["expect"].items():
                    check(out.get(key) == want,
                          f"win2 {step['tool']}.{key} == {want}", out)
            for var, field in (step.get("save") or {}).items():
                self.vars[var] = out[field]
        elif v == "transcript":
            Path(self.vars.get("transcript_dir", tempfile.gettempdir())
                 ).mkdir(parents=True, exist_ok=True)
            out = Path(self.vars.get("transcript_dir",
                                     tempfile.gettempdir())
                       ) / f"transcript-{step.get('name', 'session')}.json"
            out.write_text(json.dumps(self.transcript, indent=2))
            check(True, f"transcript → {out}")
        else:
            raise AssertionError(f"unknown verb: {v}")


async def run_session(path: Path) -> int:
    spec = json.loads(path.read_text())
    print(f"\n=== session: {spec['name']} ===", flush=True)
    home = Path(tempfile.mkdtemp(prefix="ide-home-"))
    work = Path(tempfile.mkdtemp(prefix="ide-work-"))
    clip = work / "clipboard.txt"
    env_extra = {"CONF_KNOWLEDGE_CLIPBOARD_FILE": str(clip),
                 **spec.get("env", {})}
    env_extra.setdefault("KNOWLEDGE_FAKE_EMBEDDINGS", "1")
    env_extra.setdefault("CONF_KNOWLEDGE_STEP_SECONDS", "4")

    # fake-DC shaping must land before serve() — faults/latency/corpus
    # are read by the handler at request time and by serve() at bind
    if spec.get("corpus"):
        os.environ["FAKE_DC_CORPUS"] = str(
            (path.parent / spec["corpus"]).resolve()
            if not Path(spec["corpus"]).is_absolute()
            else spec["corpus"])
    if spec.get("faults"):
        os.environ["FAKE_DC_FAULTS"] = spec["faults"]
    if spec.get("delay"):
        os.environ["FAKE_DC_DELAY"] = str(spec["delay"])
    srv, base, _hits = serve()
    ide = FakeIDE(home, work, env_extra)
    ide.vars["base"] = base
    ide.vars["token"] = TOKEN
    ide.vars["port"] = str(srv.server_port)
    ide._server = srv
    ide._serve = serve
    rc = 0
    try:
        for i, step in enumerate(spec["steps"], 1):
            try:
                await ide.run_step(step)
            except AssertionError as e:
                print(f"FAIL step {i} ({step['verb']}): {e}", flush=True)
                rc = 1
                break
            except Exception as e:   # noqa: BLE001 — an unexpected
                import traceback  # error should show its real
                 
                print(f"DIED step {i} ({step['verb']}): "
                      f"{type(e).__name__}: {e}", flush=True)
                traceback.print_exc()
                rc = 2
                break
        else:
            print(f"session {spec['name']}: all steps passed", flush=True)
    finally:
        # the stdio context's cancel-scope teardown crashes if any call
        # aborted mid-flight — the subprocess dies with the parent anyway
        try:
            await ide.kill_server()
        except Exception as e:       # noqa: BLE001 — teardown noise
            print(f"(teardown: {type(e).__name__})", flush=True)
        srv.shutdown()
        os._exit(rc)                  # skip the client's exit traceback


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: ide_replay.py sessions/<name>.json [...]")
        sys.exit(2)
    code = 0
    for arg in sys.argv[1:]:
        code |= asyncio.run(run_session(Path(arg)))
    sys.exit(code)
