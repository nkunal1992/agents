"""End-to-end smoke test: the real stdio MCP server with the real local models
against a fake Confluence Data Center served on localhost.

Runs the tool sequence the Copilot agent uses (status → connect → start_crawl →
crawl_step… → search → get_evidence → verify_citations) through the MCP client
SDK with an IDE-like per-request timeout, so protocol, threading, timeout and
packaging problems surface here instead of on a user's laptop.

Usage (models must be prepared in CONF_KNOWLEDGE_HOME first):
    CONF_KNOWLEDGE_HOME=/tmp/ck/home python scripts/e2e_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TOKEN = "e2e-secret-token-canary"
REQUEST_TIMEOUT = 20  # seconds; IDE clients time out around this order
PAGE_DELAY = 0.4      # simulated corporate-network latency per request

# root 100 → 30 children (101..130) → grandchild 200 under 101
PAGES = {"100": ("Release Handbook",
                 ("<h1>Approvals</h1><p>Every production release requires sign-off "
                 "from the Change Advisory Board.</p>"))}
CHILDREN = {"100": [str(i) for i in range(101, 131)], "101": ["200"]}
for i in range(101, 131):
    PAGES[str(i)] = (f"Team {i} notes",
                     f"<h2>Overview</h2><p>Team {i} owns service number {i}.</p>")
PAGES["200"] = ("Deploy Windows",
                ("<h2>Schedule</h2><p>Production deploys happen only on Thursdays "
                "after 18:00.</p><table><tr><th>Env</th><th>Day</th></tr>"
                "<tr><td>prod</td><td>Thursday</td></tr></table>"
                '<ac:structured-macro ac:name="info"><ac:rich-text-body>'
                "<p>The maintenance freeze runs from December 20 to January 5."
                "</p></ac:rich-text-body></ac:structured-macro>"))


class FakeDC(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        time.sleep(PAGE_DELAY)
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._json(401, {})
        path = self.path.split("?")[0].rstrip("/")
        if path == "/rest/api/user/current":
            return self._json(200, {"username": "e2e", "displayName": "E2E User"})
        parts = path.split("/")
        if len(parts) >= 5 and parts[1:4] == ["rest", "api", "content"]:
            pid = parts[4]
            if path.endswith("/child/page"):
                kids = CHILDREN.get(pid, [])
                return self._json(200, {"results": [{"id": k} for k in kids],
                                        "size": len(kids), "limit": 200})
            if pid in PAGES:
                title, body = PAGES[pid]
                anc = [] if pid == "100" else (["100", "101"] if pid == "200" else ["100"])
                return self._json(200, {
                    "id": pid, "title": title, "version": {"number": 1},
                    "body": {"storage": {"value": body}},
                    "ancestors": [{"id": a} for a in anc],
                    "_links": {"webui": f"/pages/viewpage.action?pageId={pid}",
                               "base": f"http://127.0.0.1:{self.server.server_port}"}})
        return self._json(404, {})


def check(cond, msg, detail=None):
    print(("PASS " if cond else "FAIL ") + msg + ("" if cond or detail is None else f"\n  got: {detail}"))
    if not cond:
        os._exit(1)  # skip the client's teardown traceback noise


async def call(session, name, args=None):
    t = time.monotonic()
    r = await session.call_tool(name, args or {},
                                read_timeout_seconds=timedelta(seconds=REQUEST_TIMEOUT))
    dt = time.monotonic() - t
    text = r.content[0].text if r.content else ""
    if r.isError:
        raise SystemExit(f"FAIL {name} returned an error after {dt:.1f}s: {text}")
    print(f"  {name:<17} {dt:5.1f}s")
    return json.loads(text)


async def main() -> None:
    home = Path(os.environ.get("CONF_KNOWLEDGE_HOME", "")).expanduser()
    if not (home / "models" / "all-MiniLM-L6-v2").exists():
        raise SystemExit("prepare models first: CONF_KNOWLEDGE_HOME=<dir> "
                         "confluence-knowledge prepare-models")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeDC)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    (home / "profile.json").write_text(json.dumps(
        {"env": "datacenter", "base_url": base, "email": None, "credential_ref": "e2e"}))
    work = Path(tempfile.mkdtemp(prefix="ck-e2e-work-")).resolve()
    env = {**os.environ, "CONF_KNOWLEDGE_HOME": str(home),
           "CONF_KNOWLEDGE_WORKDIR": str(work), "CONF_KNOWLEDGE_TOKEN": TOKEN,
           "CONF_KNOWLEDGE_STEP_SECONDS": "8"}
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "confluence_knowledge.server"], env=env,
                                   cwd=tempfile.gettempdir())
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        check({"status", "connect", "start_crawl", "crawl_step", "search",
               "get_evidence", "verify_citations"} <= tools, f"tools registered: {sorted(tools)}")

        st = await call(session, "status")
        check(st["configured"] and st["index_root"].startswith(str(work)),
              "status: configured, index under CONF_KNOWLEDGE_WORKDIR", st)

        c = await call(session, "connect", {"root": f"{base}/pages/viewpage.action?pageId=100"})
        scope = c["scope_handle"]
        check(not c["cached"], "connect: fresh scope, nothing cached")

        job = await call(session, "start_crawl", {"scope_handle": scope})
        steps, r = 0, {"state": "running"}
        while r["state"] != "done":
            steps += 1
            check(steps <= 40, "crawl finishes within 40 steps")
            r = await call(session, "crawl_step", {"scope_handle": scope, "job_id": job["job_id"]})
        check(r["pages_seen"] == 32 and not r["errors"],
              f"crawl: all 32 pages incl. grandchild indexed in {steps} steps", r)

        res = await call(session, "search", {"scope_handle": scope,
                                             "question": "When are production deploys allowed?"})
        top = res["evidence"][0]
        check(res["coverage"] == "complete", "search: coverage complete")
        check(top["page_title"] == "Deploy Windows" and "Thursday" in top["text"],
              f"search: grandchild ranked first ({top['page_title']})", res)
        check(top["page_url"].startswith(base + "/pages/"), "search: citation URL is absolute")

        res2 = await call(session, "search", {"scope_handle": scope,
                                              "question": "Who signs off a production release?"})
        check(res2["evidence"][0]["page_title"] == "Release Handbook",
              "search: parent-page fact retrieved")

        ev = await call(session, "get_evidence", {"scope_handle": scope,
                                                  "chunk_ids": [top["chunk_id"]]})
        check("Thursdays" in ev["chunks"][0]["text"], "get_evidence: full chunk text")

        res3 = await call(session, "search", {"scope_handle": scope,
                                              "question": "maintenance freeze dates"})
        check(any("maintenance freeze" in e["text"] for e in res3["evidence"]),
              "search: macro body content is indexed", res3["evidence"])

        ok = await call(session, "verify_citations", {
            "scope_handle": scope, "evidence_set": res["evidence_set"],
            "mappings": [{"claim": "Deploys are on Thursdays", "chunk_id": top["chunk_id"],
                          "quote": "Production deploys happen only on Thursdays"}]})
        bad = await call(session, "verify_citations", {
            "scope_handle": scope, "evidence_set": res["evidence_set"],
            "mappings": [{"claim": "Deploys are on Mondays", "chunk_id": top["chunk_id"],
                          "quote": "deploys happen on Mondays"}]})
        check(ok["ok"] and not bad["ok"], "verify_citations: accepts real quote, rejects invented one")

        again = await call(session, "connect", {"root": "100"})
        check(again["cached"] and again["cached_pages"] == 32, "reconnect: cache reused")

    leaked = [p for p in work.rglob("*") if p.is_file() and TOKEN.encode() in p.read_bytes()]
    check(not leaked, "no token written to the work dir")
    srv.shutdown()
    print("\nE2E OK")


if __name__ == "__main__":
    asyncio.run(main())
