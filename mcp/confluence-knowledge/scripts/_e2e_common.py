"""Shared fake Confluence Data Center server for e2e scripts.

Token-gated, latency-simulating DC REST adapter on 127.0.0.1 — enough of the
API surface for crawl → index → search end to end. Used by e2e_smoke.py and
e2e_bootstrap.py.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "e2e-secret-token-canary"
PAGE_DELAY = 0.4  # simulated corporate-network latency per request

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


def make_handler(hits: list[str]):
    """Request handler recording every path hit (for outbound-leak checks)."""

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
            hits.append(self.path)
            time.sleep(PAGE_DELAY)
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._json(401, {})
            path = self.path.split("?")[0].rstrip("/")
            if path == "/rest/api/user/current":
                return self._json(200, {"username": "e2e",
                                        "displayName": "E2E User"})
            parts = path.split("/")
            if len(parts) >= 5 and parts[1:4] == ["rest", "api", "content"]:
                pid = parts[4]
                if path.endswith("/child/page"):
                    kids = CHILDREN.get(pid, [])
                    return self._json(200, {
                        "results": [{"id": k} for k in kids],
                        "size": len(kids), "limit": 200})
                if pid in PAGES:
                    title, body = PAGES[pid]
                    anc = ([] if pid == "100"
                           else (["100", "101"] if pid == "200" else ["100"]))
                    return self._json(200, {
                        "id": pid, "title": title, "version": {"number": 1},
                        "body": {"storage": {"value": body}},
                        "ancestors": [{"id": a} for a in anc],
                        "_links": {
                            "webui": f"/pages/viewpage.action?pageId={pid}",
                            "base": f"http://127.0.0.1:{self.server.server_port}"}})
            return self._json(404, {})

    return FakeDC


def serve() -> tuple[ThreadingHTTPServer, str, list[str]]:
    """Start the fake DC on a random port. Returns (server, base_url, hits)."""
    hits: list[str] = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(hits))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}", hits


# --- shared MCP-client helpers (call/check used by both smoke and bootstrap) ---

import json as _json
import os as _os
import time as _time
from datetime import timedelta as _timedelta


def check(cond, msg, detail=None):
    print(("PASS " if cond else "FAIL ") + msg
          + ("" if cond or detail is None else f"\n  got: {detail}"),
          flush=True)
    if not cond:
        import sys as _sys
        _sys.stdout.flush()  # os._exit skips the buffered tail
        _os._exit(1)  # skip the client's teardown traceback noise


async def call(session, name, args=None, timeout=20):
    t = _time.monotonic()
    r = await session.call_tool(
        name, args or {}, read_timeout_seconds=_timedelta(seconds=timeout))
    dt = _time.monotonic() - t
    text = r.content[0].text if r.content else ""
    if r.isError:
        raise SystemExit(f"FAIL {name} returned an error after {dt:.1f}s: {text}")
    print(f"  {name:<17} {dt:5.1f}s")
    return _json.loads(text)
