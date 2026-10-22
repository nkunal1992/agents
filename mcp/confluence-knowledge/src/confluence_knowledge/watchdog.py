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


def watchdog_running(home: Path) -> bool:
    """True when a live watchdog holds its pid file in `home`. Pid-reuse
    makes a bare os.kill check lie — verify the cmdline too."""
    try:
        pid = int((home / _PID_FILE).read_text().strip())
        os.kill(pid, 0)
    except Exception:                           # noqa: BLE001 —
        return False                             # absent or dead
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode()
        return "confluence_knowledge.watchdog" in cmdline
    except OSError:
        return True                    # non-Linux — alive check stands


def spawn_watchdog(home: Path) -> None:
    """Detached watchdog process — survives the spawning daemon/CLI."""
    if watchdog_running(home):
        return
    env = dict(os.environ)
    env["KNOWLEDGE_WATCHDOG_ROLE"] = "1"
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "confluence_knowledge.watchdog",
             str(home)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=open(home / "watchdog.log", "ab"),   # noqa: SIM115
            env=env, start_new_session=True)
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
    for name, pid in (("daemon", rpc.daemon_pid()),
                      ("watchdog", _watchdog_pid(home))):
        if pid is None:
            continue
        try:
            os.kill(pid, _TERM)
            out[f"{name}_signalled"] = True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    deadline = time.monotonic() + timeout
    while rpc.daemon_alive() and time.monotonic() < deadline:
        time.sleep(0.2)
    out["daemon_down"] = not rpc.daemon_alive()
    return out


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
