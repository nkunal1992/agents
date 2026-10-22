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
import subprocess
import sys
import time
from pathlib import Path

from .rpc import RpcDb

log = logging.getLogger("knowledge.watchdog")

_POLL_S = 2.0
_STOP_FILE = ".watchdog.stop"
_PID_FILE = ".watchdog.pid"


def watchdog_running(home: Path) -> bool:
    """True when a live watchdog holds its pid file in `home`."""
    try:
        pid = int((home / _PID_FILE).read_text().strip())
        os.kill(pid, 0)
        return True
    except Exception:                           # noqa: BLE001 —
        return False                             # absent or dead


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
            stderr=subprocess.DEVNULL,
            env=env, start_new_session=True)
        (home / _PID_FILE).write_text(str(proc.pid))
    except Exception:
        log.warning("watchdog spawn failed", exc_info=True)


def run(home: Path) -> int:
    (home / _PID_FILE).write_text(str(os.getpid()))
    rpc = RpcDb(home / "rpc.db")
    log.info("watchdog up: home=%s", home)
    while home.exists() and not (home / _STOP_FILE).exists():
        if not rpc.daemon_alive():
            log.info("daemon lease dead — respawning")
            try:
                from .rpc import ensure_daemon
                ensure_daemon(rpc)
            except Exception:
                log.warning("respawn failed", exc_info=True)  # poll retries
        time.sleep(_POLL_S)
    try:
        (home / _PID_FILE).unlink(missing_ok=True)
    except OSError:
        pass
    return 0


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
