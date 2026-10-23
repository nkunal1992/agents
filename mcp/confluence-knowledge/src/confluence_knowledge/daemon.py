"""The daemon — the long-running 'brain' behind the thin MCP proxy (#189),
and the MCP endpoint itself over localhost HTTP (#206).

One process per interpreter serves every installed knowledge service:
entry-point discovery (`knowledge.services`) mounts each importable
server module's FastMCP app at `/<service>/mcp` under a single uvicorn,
so installing all three agents on a shared venv costs ONE process, ONE
port and ONE model pool — not three. A standalone install still works:
discovery finds its single service and the code path is identical.

    python -m confluence_knowledge.daemon <daemon_home> [--module <mod>]

- Acquires the service-level lease in rpc.db (job_lease semantics,
  one level up) — a dead pid or stale heartbeat hands it over.
- Polls `rpc_requests` every ~150 ms, dispatches to the request's
  `service` column → mounted module (stdio-proxy path; http calls run
  natively inside the mounted app, no rpc hop).
- Resumes orphaned running jobs on boot per mounted module.
- Serves `/_services` — registered mounts — so `register` can detect a
  daemon that predates a later install and restart it.
- Snooze: the shared ModelPool reaper (KNOWLEDGE_IDLE_UNLOAD_S, default
  30 min) releases models while the daemon stays alive at baseline RSS.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
import traceback
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from .rpc import RpcDb, daemon_owner

log = logging.getLogger("knowledge.daemon")

_HEARTBEAT_S = 1.0
_POLL_S = 0.15


def _port_free(port: int) -> bool:
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pkg_version() -> str:
    """The installed engine version the daemon's code came from — lets a
    status check tell 'daemon running 0.11.0 code, package 0.11.1' so an
    upgrade actually reaches the running process (#222 follow-up)."""
    try:
        return importlib.metadata.version("confluence-knowledge")
    except Exception:                           # noqa: BLE001 — editable
        return "unknown"


def _bearer_app(app, token: str):
    """ASGI middleware — loopback is still a multi-user surface; every
    request must carry the per-venv bearer from http.json."""
    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            want = f"bearer {token}".encode()
            got = next((v for k, v in scope["headers"]
                        if k.lower() == b"authorization"), b"")
            if got.lower() != want:
                # JetBrains Copilot silently drops mcp.json headers
                # (upstream #1579) — its entries carry the token as a
                # ?token= query param instead; loopback-only either way
                from urllib.parse import parse_qs
                qs = parse_qs(scope.get("query_string", b"").decode())
                if (qs.get("token") or [""])[0] == token:
                    await app(scope, receive, send)
                    return
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body",
                            "body": b"unauthorized"})
                return
        await app(scope, receive, send)
    return wrapped


def discover_services(only_module: str | None = None) -> dict[str, object]:
    """{mount_name: server_module} for every knowledge service importable
    in this interpreter. Entry points declared in each pyproject —
    a foreign-venv package simply isn't there, which is exactly the
    shared-vs-standalone boundary (#206)."""
    out: dict[str, object] = {}
    try:
        eps = importlib.metadata.entry_points(group="knowledge.services")
    except Exception:                       # noqa: BLE001 — old python
        eps = []
    for ep in eps:
        if only_module and ep.value != only_module:
            continue
        try:
            out[ep.name] = importlib.import_module(ep.value)
        except Exception:
            log.warning("service %r failed to import", ep.name,  # mount
                        exc_info=True)                          # skips
    if not out and only_module:
        # legacy: a module path with no entry point — mount it under its
        # package stem so the old CLI form keeps working
        name = only_module.split(".")[0].removesuffix("_knowledge")
        out[name] = importlib.import_module(only_module)
    return out


class Daemon:
    def __init__(self, workdir: Path, only_module: str | None = None):
        self.workdir = workdir
        self.only_module = only_module
        self.owner = daemon_owner()
        # a watchdog respawn can boot while the previous daemon still
        # holds the WAL lock — transient lock on open, retry briefly
        import sqlite3
        self.rpc = None
        for _ in range(30):
            try:
                self.rpc = RpcDb(workdir / "rpc.db")
                break
            except sqlite3.OperationalError:
                time.sleep(0.2)
        if self.rpc is None:
            self.rpc = RpcDb(workdir / "rpc.db")   # final attempt raises
        self.stop = threading.Event()
        self._inflight: list[threading.Thread] = []
        self._http = None
        self.services: dict[str, object] = {}

    # ------------------------------------------------------------------
    def run(self) -> int:
        self.services = discover_services(self.only_module)
        if not self.services:
            log.error("no knowledge services importable — nothing to serve")
            return 4
        for srv in self.services.values():
            # same process-level tuning the stdio server does for itself;
            # every service maps to the shared pool, so one start covers all
            try:
                if hasattr(srv, "_pool") and \
                        hasattr(srv._pool, "start_reaper"):
                    srv._pool.start_reaper()
            except Exception:               # noqa: BLE001,S112 — tuning
                continue                     # is best-effort per module
        try:
            from .resources import lower_priority
            lower_priority()
        except Exception:                       # noqa: BLE001,S110 —
            pass                                 # tuning is best-effort
        if not self.rpc.acquire_daemon(self.owner, os.getpid()):
            log.error("daemon lease held by a live process — exiting")
            return 2
        log.info("daemon up: owner=%s workdir=%s services=%s",
                 self.owner, self.workdir, sorted(self.services))
        self._http = self._serve_http()
        self._resume_orphans()
        last_gc = 0.0
        last_probe = 0.0
        probe_fails = 0
        from .watchdog import spawn_watchdog
        spawn_watchdog(self.workdir)   # it revives us; we revive it
        try:
            while not self.stop.is_set():
                alive = self.rpc.heartbeat_daemon(self.owner)
                if not alive:
                    log.error("lease lost — another daemon took over; exit")
                    return 3
                for rid, tool, args, service in \
                        self.rpc.poll_requests(self.owner):
                    self._dispatch(rid, tool, args, service)
                if time.time() - last_gc > 60:
                    last_gc = time.time()
                    self._gc_leases()
                    spawn_watchdog(self.workdir)  # revive a dead sibling
                if time.time() - last_probe > 10:
                    last_probe = time.time()
                    if self._probe_http():
                        probe_fails = 0
                    else:
                        probe_fails += 1
                        if probe_fails >= 2:
                            # the MCP surface is wedged (e.g. the session
                            # manager died across an OS suspend) — uvicorn
                            # still answers TCP so nothing else can see
                            # it. Self-eject: the watchdog respawns us on
                            # the SAME url; one client retry, no reload.
                            log.error("mcp surface wedged — self-eject "
                                      "for watchdog respawn")
                            return 5
                time.sleep(_POLL_S)
        finally:
            if self._http is not None:
                self._http.should_exit = True
            self.rpc.release_daemon(self.owner)
        return 0

    # ------------------------------------------------------------------
    def _serve_http(self):
        """Mount every discovered service's FastMCP app under
        /<service>/ — one port, reconnect-per-request. Chained lifespans:
        Starlette Mount does not propagate, so the parent's lifespan
        enters each sub-app's (the session managers' task groups)."""
        try:
            import uvicorn
            from starlette.applications import Starlette
            from starlette.responses import JSONResponse
            from starlette.routing import Mount, Route

            meta = self._http_meta()
            apps = {name: srv.mcp.streamable_http_app()
                    for name, srv in self.services.items()}

            @asynccontextmanager
            async def lifespan(_app):
                async with AsyncExitStack() as stack:
                    for app in apps.values():
                        await stack.enter_async_context(
                            app.router.lifespan_context(app))
                    yield

            async def services_endpoint(_request):
                return JSONResponse({"services": sorted(apps),
                                     "version": _pkg_version()})

            routes = [Mount(f"/{name}", app=app)
                      for name, app in apps.items()]
            routes.append(Route("/_services", services_endpoint))
            parent = Starlette(routes=routes, lifespan=lifespan)

            cfg = uvicorn.Config(
                _bearer_app(parent, meta["token"]),
                host="127.0.0.1", port=meta["port"],
                log_level="warning", access_log=False)
            server = uvicorn.Server(cfg)
            threading.Thread(target=server.run, daemon=True,
                             name="mcp-http").start()
            deadline = time.monotonic() + 8
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.05)
            if not server.started:
                log.error("http server did not start — rpc dispatch only")
                return None
            self._http_meta_v = meta
            log.info("mcp http endpoints: %s",
                     {n: f"http://127.0.0.1:{meta['port']}/{n}/mcp"
                      for n in apps})
            return server
        except Exception:
            log.exception("http endpoint failed — rpc dispatch only")
            return None                          # optional

    def _probe_http(self) -> bool:
        """POST a real initialize at our own endpoint — the health check
        that catches 'uvicorn alive, session manager dead' after a long
        OS suspend. One mounted service is enough; they share the
        manager lifecycle."""
        meta = getattr(self, "_http_meta_v", None)
        if not meta or not self.services:
            return True                      # nothing served to check
        import urllib.request
        body = json.dumps({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26",
                       "capabilities": {},
                       "clientInfo": {"name": "self-probe",
                                      "version": "0"}}}).encode()
        svc = min(self.services)
        req = urllib.request.Request(
            f"http://127.0.0.1:{meta['port']}/{svc}/mcp", data=body,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "Authorization": f"Bearer {meta['token']}"})
        try:
            # localhost-only probe — the URL is built above from our own
            # http.json; assert the scheme/host so a tampered meta can't
            # smuggle file:/ or a remote origin into urllib
            if not req.full_url.startswith("http://127.0.0.1:"):
                log.warning("mcp self-probe refused non-local URL")
                return False
            with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310
                return r.status == 200
        except Exception:
            log.warning("mcp self-probe failed", exc_info=True)
            return False

    def _http_meta(self) -> dict:
        """Resolve {port, token} for the endpoint, persisted in the daemon
        home so a restart re-binds the SAME urls the IDE registered."""
        path = self.workdir / "http.json"
        meta = {}
        try:
            meta = json.loads(path.read_text())
        except Exception:                       # noqa: BLE001,S110
            pass
        token = meta.get("token") or secrets.token_hex(16)
        port = meta.get("port")
        if not (isinstance(port, int) and _port_free(port)):
            port = _free_port()
        path.write_text(json.dumps({"port": port, "token": token}))
        os.chmod(path, 0o600)
        return meta | {"port": port, "token": token}

    # ------------------------------------------------------------------
    def _dispatch(self, rid: str, tool: str, args: dict,
                  service: str = "") -> None:
        srv = self.services.get(service)
        if srv is None:
            # legacy requests carry no service — dispatch to the only
            # mount when unambiguous, else a named miss
            srv = (next(iter(self.services.values()))
                   if len(self.services) == 1 else None)
        fn = getattr(srv, tool, None) if srv is not None else None
        if fn is None or not callable(fn):
            self.rpc.respond(rid, error=f"unknown daemon tool {tool!r} "
                                        f"(service {service!r})")
            return

        def work() -> None:
            try:
                self.rpc.respond(rid, result=fn(**args))
            except Exception as e:              # noqa: BLE001 — the
                log.warning("tool %s failed: %s", tool, e)   # proxy needs
                self.rpc.respond(rid, error=f"{type(e).__name__}: {e}")
                log.debug(traceback.format_exc())

        t = threading.Thread(target=work, daemon=True, name=f"rpc-{tool}")
        t.start()
        self._inflight.append(t)
        self._inflight = [x for x in self._inflight if x.is_alive()]

    def _resume_orphans(self) -> None:
        """Jobs left 'running' by a dead daemon get their workers back —
        checked per mounted module's index root."""
        for name, srv in self.services.items():
            try:
                index_root = srv.index_root()
            except Exception:                   # noqa: BLE001,S112 —
                continue                        # unmounted service root
            for db in sorted(index_root.glob("*.db")):
                if db.name == "rpc.db":
                    continue
                try:
                    idx = srv._index_for(db.stem)
                except Exception:               # noqa: BLE001,S112 —
                    continue                    # damaged index → skipped
                try:
                    jobs = idx.db.execute(
                        "SELECT job_id FROM jobs WHERE state='running'"
                    ).fetchall()
                except Exception:               # noqa: BLE001,S112 —
                    continue                     # unreadable jobs table
                for (job_id,) in jobs:
                    log.info("resuming orphaned job %s on %s (%s)",
                             job_id, db.stem, name)
                    try:
                        srv._resume_job(db.stem, job_id)
                    except Exception:           # noqa: BLE001
                        log.warning("orphan resume failed for %s", job_id)

    def _gc_leases(self) -> None:
        """Periodic housekeeping — nothing yet; hook for sweeps."""
        return


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s daemon %(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    module = None
    if "--module" in sys.argv:
        i = sys.argv.index("--module")
        module = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    if not args:
        print("usage: python -m confluence_knowledge.daemon "
              "<daemon_home> [--module <mod>]", file=sys.stderr)
        return 2
    return Daemon(Path(args[0]), only_module=module).run()


if __name__ == "__main__":
    sys.exit(main())
