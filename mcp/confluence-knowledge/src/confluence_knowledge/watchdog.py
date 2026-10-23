"""The watchdog — a tiny sibling process that keeps the daemon alive
(#206). Under HTTP there is no per-request proxy to lazily respawn a
crashed daemon; the IDE only knows a URL, so a dead daemon is dead tools
until something restarts it. The watchdog is that something:

- Every ~2 s: if the daemon lease is dead, spawn it (the daemon's own
  acquire_daemon makes racing double-spawns self-correcting).
- The daemon reciprocally respawns the watchdog on its heartbeat loop —
  mutual supervision; either can die alone and come back.
- Exits cleanly when `.watchdog.stop` appears (doctor/user stop) or the
  daemon home is deleted. ~15 MB RSS, no models, no index handles.

    python -m confluence_knowledge.watchdog <daemon_home>
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .rpc import RpcDb

log = logging.getLogger("knowledge.watchdog")

_POLL_S = 2.0
_STOP_FILE = ".watchdog.stop"
_TERM = signal.SIGTERM
_PID_FILE = ".watchdog.pid"


def _cmdline_is(pid: int, needle: str) -> bool:
    """Pid-file entries can outlive their process — pid reuse hands the
    number to an innocent program, so a bare os.kill is wrong. Verify
    the cmdline before signalling (mirrors watchdog_running's check)."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode()
        return needle in cmdline
    except OSError:
        try:
            import psutil
            return needle in " ".join(
                psutil.Process(pid).cmdline())
        except Exception:                       # noqa: BLE001 — can't
            return False                        # verify → don't signal


def watchdog_running(home: Path) -> bool:
    """True when a live watchdog holds its pid file in `home`. Pid-reuse
    makes a bare os.kill check lie — verify the cmdline too. On macOS a
    STALE pid file pointing at a live, unrelated process must not count
    (no /proc — the old 'alive check stands' path suppressed spawns)."""
    try:
        pid = int((home / _PID_FILE).read_text().strip())
        os.kill(pid, 0)
    except Exception:                           # noqa: BLE001 —
        return False                             # absent or dead
    return _cmdline_is(pid, "confluence_knowledge.watchdog")


def spawn_watchdog(home: Path) -> None:
    """Detached watchdog process — survives the spawning daemon/CLI."""
    if watchdog_running(home):
        return
    env = dict(os.environ)
    env["KNOWLEDGE_WATCHDOG_ROLE"] = "1"
    try:
        from .rpc import _popen_detached, detached_spawn_kwargs
        proc = _popen_detached(
            [sys.executable, "-m", "confluence_knowledge.watchdog",
             str(home)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=open(home / "watchdog.log", "ab"),   # noqa: SIM115
            env=env, **detached_spawn_kwargs())
        (home / _PID_FILE).write_text(str(proc.pid))
    except Exception:
        log.warning("watchdog spawn failed", exc_info=True)


def run(home: Path) -> int:
    (home / _PID_FILE).write_text(str(os.getpid()))
    rpc = RpcDb(home / "rpc.db")
    log.info("watchdog up: home=%s", home)
    fails = 0
    while home.exists() and not (home / _STOP_FILE).exists():
        if not rpc.daemon_alive():
            # a `stop` may have landed mid-iteration — re-check before
            # respawning so the shutdown is honoured, not resurrected
            if (home / _STOP_FILE).exists():
                break
            if fails >= 3:
                # e.g. the package was uninstalled — respawning a
                # crash-loop forever is worse than standing down
                log.error("3 consecutive respawn failures — exiting")
                break
            log.info("daemon lease dead — respawning")
            try:
                from .rpc import ensure_daemon
                ensure_daemon(rpc)
                fails = 0
            except Exception:
                fails += 1
                log.warning("respawn failed (%d/3)", fails,
                            exc_info=True)
        time.sleep(_POLL_S)
    try:
        (home / _PID_FILE).unlink(missing_ok=True)
    except OSError:
        pass
    return 0


def _daemon_pid_file(home: Path) -> int | None:
    try:
        pid = int((home / ".daemon.pid").read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if _cmdline_is(pid, "confluence_knowledge.daemon") else None


def _watchdog_pid(home: Path) -> int | None:
    try:
        pid = int((home / _PID_FILE).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if watchdog_running(home) else None


def stop_daemon(home: Path, timeout: float = 8) -> dict:
    """The user's intentional shutdown — the ONLY way a kill survives:
    `.watchdog.stop` tells the watchdog to stand down (otherwise it would
    respawn the daemon), then SIGTERM the daemon itself."""
    out = {"stop_file": False, "daemon_signalled": False,
           "watchdog_signalled": False}
    (home / _STOP_FILE).write_text("1")
    out["stop_file"] = True
    rpc = RpcDb(home / "rpc.db")
    # boot race: the daemon may still be starting (no lease yet) — the
    # pid FILE is written at spawn time, before the lease exists
    pid = rpc.daemon_pid()
    if pid is None:
        pid = _daemon_pid_file(home)   # stale-file path — cmdline verified
    daemon_pid = (pid if pid is not None and
                  _cmdline_is(pid, "confluence_knowledge.daemon")
                  else None)
    for name, pid_ in (("daemon", daemon_pid),
                       ("watchdog", _watchdog_pid(home))):
        if pid_ is None:
            continue
        try:
            os.kill(pid_, _TERM)
            out[f"{name}_signalled"] = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    deadline = time.monotonic() + timeout
    while rpc.daemon_alive() and time.monotonic() < deadline:
        time.sleep(0.2)
    out["daemon_down"] = not rpc.daemon_alive()
    return out


def restart_daemon(home: Path, timeout: float = 8) -> None:
    """Doctor/upgrade path — stop the pair and spawn a fresh daemon, then
    CLEAR the stop-file so the new watchdog actually runs. Distinct from
    `stop_daemon`'s "user wants it off" semantics: here the intent is
    restart, so the marker must not survive or the watchdog refuses to
    loop and the daemon ends up watchdog-less (#226)."""
    stop_daemon(home, timeout=timeout)
    (home / _STOP_FILE).unlink(missing_ok=True)
    from .rpc import RpcDb, ensure_daemon
    ensure_daemon(RpcDb(home / "rpc.db"))
    spawn_watchdog(home)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s watchdog %(message)s")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: python -m confluence_knowledge.watchdog "
              "<daemon_home>", file=sys.stderr)
        return 2
    return run(Path(args[0]))


if __name__ == "__main__":
    sys.exit(main())
