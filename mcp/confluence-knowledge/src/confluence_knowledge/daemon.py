"""The daemon — the long-running 'brain' behind the thin MCP proxy (#189).

Owns the index write connection, the crawl workers, and the model pool;
proxies keep zero of those. Lifecycle:

    python -m confluence_knowledge.daemon <server_module> <workdir>

- Acquires the service-level lease in rpc.db (job_lease semantics,
  one level up) — a dead pid or stale heartbeat hands it over.
- Polls `rpc_requests` every ~150 ms, dispatches to the server module's
  existing sync tool functions in a worker pool, writes `rpc_responses`.
- Resumes orphaned running jobs on boot: a daemon crash mid-crawl picks
  up where the durable queue/checkpoint left it — the proxies never see
  a restart.
- Snooze: the server's ModelPool reaper (KNOWLEDGE_IDLE_UNLOAD_S,
  default 30 min) releases models while the daemon stays alive at
  baseline RSS — next search reloads lazily.
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from .rpc import RpcDb, daemon_owner

log = logging.getLogger("knowledge.daemon")

_HEARTBEAT_S = 1.0
_POLL_S = 0.15

# the daemon can serve ANY tool — reads included — so a proxy whose
# direct read fails falls over to it (the user's safeguard 3). Mutations
# MUST go here; reads merely may.
DAEMON_TOOLS = None   # None = dispatch any attribute on the module


class Daemon:
    def __init__(self, server_module: str, workdir: Path):
        self.module_name = server_module
        self.workdir = workdir
        self.owner = daemon_owner()
        self.rpc = RpcDb(workdir / "rpc.db")
        self.stop = threading.Event()
        self._inflight: list[threading.Thread] = []

    # ------------------------------------------------------------------
    def run(self) -> int:
        srv = importlib.import_module(self.module_name)
        # same process-level tuning the stdio server does for itself
        if hasattr(srv, "_pool") and hasattr(srv._pool, "start_reaper"):
            srv._pool.start_reaper()
        try:
            from .resources import lower_priority
            lower_priority()
        except Exception:                       # noqa: BLE001,S110 —
            pass                                 # tuning is best-effort
        if not self.rpc.acquire_daemon(self.owner, os.getpid()):
            log.error("daemon lease held by a live process — exiting")
            return 2
        log.info("daemon up: owner=%s workdir=%s", self.owner, self.workdir)
        self._resume_orphans(srv)
        last_gc = 0.0
        try:
            while not self.stop.is_set():
                alive = self.rpc.heartbeat_daemon(self.owner)
                if not alive:
                    log.error("lease lost — another daemon took over; exit")
                    return 3
                for rid, tool, args in self.rpc.poll_requests(self.owner):
                    self._dispatch(srv, rid, tool, args)
                if time.time() - last_gc > 60:
                    last_gc = time.time()
                    self._gc_leases(srv)
                time.sleep(_POLL_S)
        finally:
            self.rpc.release_daemon(self.owner)
        return 0

    # ------------------------------------------------------------------
    def _dispatch(self, srv, rid: str, tool: str, args: dict) -> None:
        fn = getattr(srv, tool, None)
        if fn is None or not callable(fn):
            self.rpc.respond(rid, error=f"unknown daemon tool {tool!r}")
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

    def _resume_orphans(self, srv) -> None:
        """Jobs left 'running' by a dead daemon get their workers back —
        same durable-queue resume the KILL scenario verifies."""
        try:
            index_root = srv.index_root()
        except Exception:                       # noqa: BLE001
            return
        for db in sorted(index_root.glob("*.db")):
            if db.name == "rpc.db":
                continue
            try:
                idx = srv._index_for(db.stem)
            except Exception:                   # noqa: BLE001,S112 —
                continue                        # damaged index → skipped
            try:
                jobs = idx.db.execute(
                    "SELECT job_id FROM jobs WHERE state='running'"
                ).fetchall()
            except Exception:                   # noqa: BLE001,S112 —
                continue                         # unreadable jobs table
            for (job_id,) in jobs:
                log.info("resuming orphaned job %s on %s", job_id, db.stem)
                try:
                    srv._resume_job(db.stem, job_id)
                except Exception:               # noqa: BLE001
                    log.warning("orphan resume failed for %s", job_id)

    def _gc_leases(self, srv) -> None:
        """Periodic housekeeping — nothing yet; hook for sweeps."""
        return


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s daemon %(message)s")
    if len(sys.argv) != 3:
        print("usage: python -m confluence_knowledge.daemon "
              "<server_module> <workdir>", file=sys.stderr)
        return 2
    d = Daemon(sys.argv[1], Path(sys.argv[2]))
    return d.run()


if __name__ == "__main__":
    sys.exit(main())
