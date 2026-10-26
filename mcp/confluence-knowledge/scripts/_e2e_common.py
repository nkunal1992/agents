"""Shared fake Confluence Data Center server for e2e scripts.

Token-gated, latency-simulating DC REST adapter on 127.0.0.1 — enough of the
API surface for crawl → index → search end to end. Used by e2e_smoke.py and
e2e_bootstrap.py.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

TOKEN = "e2e-secret-token-canary"
PAGE_DELAY = float(os.environ.get("FAKE_DC_DELAY", "0.4"))
                        # simulated corporate-network latency per request

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


def _load_corpus() -> tuple[dict, dict, dict]:
    """FAKE_DC_CORPUS=<fixture.json> swaps the baked corpus for harvested
    real pages. Returns (PAGES, CHILDREN, PARENT_OF) — parent chains are
    re-derived by inverting children (the fixture carries children only).
    """
    import os
    path = os.environ.get("FAKE_DC_CORPUS")
    if not path:
        return PAGES, CHILDREN, {}
    d = json.loads(Path(path).read_text(encoding="utf-8"))["pages"]
    pages = {pid: (p.get("title", pid), p.get("body", ""))
             for pid, p in d.items()}
    children = {pid: p.get("children", []) for pid, p in d.items()}
    parent_of = {k: pid for pid, p in d.items()
                 for k in p.get("children", [])}
    return pages, children, parent_of


def _faults() -> list[tuple[str, str]]:
    """FAKE_DC_FAULTS env — hostile-server replay for negative-path
    e2e. Spec: comma-separated 'kind:match' rules; match is a path
    substring ('content/115') or 'after=N' (fires once request count
    reaches N, then keeps firing).

      403:content/115   body denied — page lists but can't be fetched
      500:content/120   server error on that page
      429:after=30      rate-limit kicks in after 30 requests
      drop:content/118  connection reset mid-response
      delay:content/110  +5 s before answering that page
      hang:content/125  never answers (tests client timeouts)
      stale_token:after=40  auth revoked mid-crawl
    """
    import os as _os
    spec = _os.environ.get("FAKE_DC_FAULTS", "")
    out = []
    for rule in spec.split(","):
        rule = rule.strip()
        if ":" in rule:
            kind, match = rule.split(":", 1)
            out.append((kind.strip(), match.strip()))
    return out


_FAULT_COUNT = [0]           # shared request counter for :after= rules


def make_handler(hits: list[str]):
    """Request handler recording every path hit (for outbound-leak checks)."""
    rules = _faults()

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

        def _apply_faults(self) -> bool:
            """True if a fault consumed the request (don't route it)."""
            path = self.path.split("?")[0]
            _FAULT_COUNT[0] += 1
            for kind, match in rules:
                hit = (path.endswith(match) if not match.startswith("after=")
                       else _FAULT_COUNT[0] >= int(match[6:]))
                if not hit:
                    continue
                if kind == "drop":
                    self.close_connection = True
                    return True               # socket closed, no response
                if kind == "delay":
                    time.sleep(5)
                    continue
                if kind == "hang":
                    time.sleep(120)           # outlives any client timeout
                    return True
                if kind == "stale_token":
                    return self._json(401, {})
                if kind.isdigit():
                    return self._json(int(kind), {})
            return False

        def do_GET(self):
            hits.append(self.path)
            time.sleep(float(os.environ.get("FAKE_DC_DELAY",
                                            str(PAGE_DELAY))))
            if self._apply_faults():
                return
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
                    from urllib.parse import parse_qs, urlparse
                    q = parse_qs(urlparse(self.path).query)
                    start = int(q.get("start", ["0"])[0])
                    limit = int(q.get("limit", ["200"])[0])
                    kids = CHILDREN_T.get(pid, [])
                    window = kids[start:start + limit]
                    return self._json(200, {
                        "results": [{"id": k} for k in window],
                        "size": len(window), "limit": limit})
                if pid in PAGES_T:
                    title, body = PAGES_T[pid]
                    anc = []
                    cur = pid
                    while cur in PARENT_OF:        # re-derive ancestors
                        cur = PARENT_OF[cur]
                        anc.append(cur)
                    return self._json(200, {
                        "id": pid, "title": title,
                        "version": {"number": 1},
                        "body": {"storage": {"value": body}},
                        "ancestors": [{"id": a} for a in reversed(anc)],
                        "_links": {
                            "webui": f"/pages/viewpage.action?pageId={pid}",
                            "base": f"http://127.0.0.1:{self.server.server_port}"}})
            return self._json(404, {})

    PAGES_T, CHILDREN_T, PARENT_OF = _load_corpus()
    return FakeDC


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, str, list[str]]:
    """Start the fake DC. port=0 picks a free one; pass the same port to
    rebind after kill_confluence (the server-flap scenario — the scope's
    base_url is pinned to the port)."""
    hits: list[str] = []
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(hits))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}", hits



# --- venv hygiene ---------------------------------------------------
# ensurepip ships pip<26.2.2 whose vendored certifi trusts the emSign
# intermediates with compromised keys (GHSA-6q48-6c7w-9gw2). A fresh
# venv must NOT pip-install anything before pip+certifi are current.


def venv_bin(venv_dir: Path) -> Path:
    """bin/ vs Scripts/ — the dir holding the venv's executables."""
    import sys as _s
    return venv_dir / ("Scripts" if _s.platform == "win32" else "bin")


def fresh_venv(venv_dir: Path) -> Path:
    """Create a venv and secure its installer before anything else runs —
    upgrade pip>=26.2.2 + certifi>=2026.10.19 inside the venv so the
    first `pip install` cannot chain through the compromised emSign
    intermediates."""
    import shutil as _sh
    import subprocess as _sp
    import venv as _v
    # an interrupted earlier run can leave a half-installed pip whose
    # dist-info makes ensurepip skip the reinstall — always start clean
    if venv_dir.exists():
        _sh.rmtree(venv_dir)
    _v.EnvBuilder(with_pip=True).create(venv_dir)
    py = str(venv_bin(venv_dir) / "python")
    cmd = [py, "-m", "pip", "install", "--quiet", "-U",
           "pip>=26.2.2", "certifi>=2026.10.19"]
    # the venv's own pip still trusts emSign until upgraded — pin the
    # TLS roots to the HOST's (already-fixed) certifi for this call so
    # the bootstrap install itself can't chain to the bad intermediates
    try:
        import certifi as _cert
        cmd += ["--cert", _cert.where()]
    except Exception:                       # noqa: BLE001,S110 —
        pass                                # cert pin is best-effort
    _sp.run(cmd, check=True)
    return venv_dir


# --- shared MCP-client helpers (call/check used by both smoke and bootstrap) ---

import json as _json
import time as _time
from datetime import timedelta as _timedelta


def check(cond, msg, detail=None):
    line = ("PASS " if cond else "FAIL ") + msg \
        + ("" if cond or detail is None else f"\n  got: {detail}")
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # Windows runner consoles are cp1252 — arrows/checkmarks in doctor
        # output crash print(); degrade to ? rather than fail the e2e
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
    if not cond:
        # SystemExit, not os._exit: finally blocks and atexit hooks must
        # run so sandboxed daemons/watchdogs get reaped — a bare _exit
        # orphaned them and past runs piled up dozens of respawning
        # pairs until the host ran out of threads entirely
        raise SystemExit(1)


def stop_shared_daemon(cli: Path, env: dict) -> None:
    """CI leaves no orphans: register/doctor spawn the shared daemon +
    watchdog under the script's sandboxed KNOWLEDGE_SHARED_HOME — the
    runner's post-checkout cleanup would otherwise have to kill them
    (and on Windows that marks the job failed). Best-effort, never
    raises — called via atexit by every e2e script."""
    try:
        import subprocess as _sp
        _sp.run([str(cli), "stop"], env=env, capture_output=True,
                timeout=30, check=False)
        # dump the daemon pair's tails — CI artifacts can't see the
        # sandboxed KNOWLEDGE_SHARED_HOME, so this is the only place
        # the logs ever surface
        import glob as _g
        shared = env.get("KNOWLEDGE_SHARED_HOME") or \
            str(Path(env.get("HOME", "")) / ".knowledge")
        for pat in ("**/daemon.log", "**/watchdog.log"):
            for f in _g.glob(str(Path(shared) / pat), recursive=True):
                try:
                    lines = Path(f).read_text(
                        errors="replace", encoding="utf-8").splitlines()
                    print(f"---- {f} (last 40) ----")
                    print("\n".join(lines[-40:]))
                except OSError:
                    pass
    except Exception:                       # noqa: BLE001,S110 —
        pass                                 # teardown is best-effort


async def call(session, name, args=None, timeout=20):
    t = _time.monotonic()
    r = await session.call_tool(
        name, args or {}, read_timeout_seconds=_timedelta(seconds=timeout))
    dt = _time.monotonic() - t
    text = r.content[0].text if r.content else ""
    if r.isError:
        raise SystemExit(f"FAIL {name} returned an error after {dt:.1f}s: {text}")
    print(f"  {name:<17} {dt:5.1f}s")
    out = _json.loads(text)
    if isinstance(out, dict) and (out.get("_error") or out.get("error")):
        print(f"  ! {name} returned error payload: {out}", flush=True)
    return out
