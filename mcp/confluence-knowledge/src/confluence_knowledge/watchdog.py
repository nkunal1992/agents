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

from .rpc import RpcDb, _cmdline_is

log = logging.getLogger("knowledge.watchdog")

_POLL_S = 2.0
_STOP_FILE = ".watchdog.stop"
_TERM = signal.SIGTERM
_PID_FILE = ".watchdog.pid"


def watchdog_running(home: Path) -> bool:
    """True when a live watchdog holds its pid file in `home`. Pid-reuse
    makes a bare os.kill check lie — verify the cmdline too. On macOS a
    STALE pid file pointing at a live, unrelated process must not count
    (no /proc — the old 'alive check stands' path suppressed spawns).
    Windows note: os.kill(pid, 0) is CTRL_C_EVENT there — it can throw on
    a detached sibling or TERMINATE it outright (verified on the QA VM),
    either way the check lies and every kick spawns another watchdog.
    psutil.pid_exists is the non-destructive cross-platform probe."""
    try:
        pid = int((home / _PID_FILE).read_text().strip())
    except Exception:                           # noqa: BLE001 —
        return False                             # absent/unreadable
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
    except ImportError:
        if os.name == "nt":
            pass                     # can't probe on Windows without psutil —
        else:                        # trust the pid file + cmdline check
            try:
                os.kill(pid, 0)                  # managed-ok — POSIX
            except Exception:                    # noqa: BLE001 —
                return False                     # fallback only
    return _cmdline_is(pid, "confluence_knowledge.watchdog")


def spawn_watchdog(home: Path, live_daemon: bool = False) -> None:
    """Detached watchdog process — survives the spawning daemon/CLI.
    `live_daemon` marks calls made by the daemon itself: it is alive and
    asking for supervision, so a leftover `.watchdog.stop` is stale and
    gets cleared BEFORE the running-check — otherwise a supervisor on
    its way out would satisfy the check while still exiting (#253: e2e
    teardown's stop file idled the next generation's watchdog)."""
    if os.environ.get("KNOWLEDGE_WATCHDOG_ROLE"):
        return                       # a watchdog never spawns a sibling
    if watchdog_running(home) and not live_daemon:
        return
    env = dict(os.environ)
    env["KNOWLEDGE_WATCHDOG_ROLE"] = "1"
    try:
        from .rpc import _popen_detached, _spawn_lock, detached_spawn_kwargs
        # the same file mutex that serializes daemon spawns (#253) —
        # a daemon-revive and a restart_daemon racing here must not both
        # pass watchdog_running and land duplicate watchdogs
        with _spawn_lock(home):
            if live_daemon:
                (home / _STOP_FILE).unlink(missing_ok=True)
            if watchdog_running(home):     # re-check under the lock
                return
            proc = _popen_detached(
                [sys.executable, "-m", "confluence_knowledge.watchdog",
                 str(home)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=open(home / "watchdog.log", "ab"),  # noqa: SIM115
                env=env, **detached_spawn_kwargs())
            (home / _PID_FILE).write_text(str(proc.pid))
    except Exception:
        log.warning("watchdog spawn failed", exc_info=True)


def run(home: Path) -> int:
    (home / _PID_FILE).write_text(str(os.getpid()))
    rpc = RpcDb(home / "rpc.db")
    log.info("watchdog up: home=%s", home)
    fails = 0
    # the stop file stands a watchdog down only for a DEAD daemon — a
    # stale marker (leftover teardown, crashed `stop`) must never idle
    # a supervisor while its daemon is alive and beating (#253)
    while home.exists():
        # supersede check: the pid file names exactly ONE live watchdog —
        # if a spawn race or a stale-check miss let a second one boot,
        # whoever isn't named must exit rather than pile up forever
        try:
            if int((home / _PID_FILE).read_text().strip()) != os.getpid():
                log.info("superseded by another watchdog — exiting")
                break
        except (OSError, ValueError):
            pass            # transient partial write/missing — next poll
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
                ensure_daemon(rpc, await_up=False)   # the 2s loop IS
                fails = 0                             # the wait — don't
            except Exception:                         # sit in _await_daemon
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


def _pids_for(home: Path, module: str) -> set[int]:
    """Every pid whose cmdline is `<module> <home>` — #288: on uv-style
    venvs `python.exe` is a launcher stub, so one logical daemon appears
    as TWO processes (the stub's Popen pid + the real interpreter it
    spawned). Pid files and the lease hold different halves of that
    pair; enumerating by cmdline is the only way to cover both."""
    out: set[int] = set()
    try:
        import psutil
    except ImportError:
        return out
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if proc.pid == me:
                continue
            argv = proc.cmdline() or []
            if module in " ".join(argv) and str(home) in argv:
                out.add(proc.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied,
                psutil.ZombieProcess):
            continue
    return out


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
    daemon_pids = _pids_for(home, "confluence_knowledge.daemon")
    if pid is not None and _cmdline_is(pid, "confluence_knowledge.daemon"):
        daemon_pids.add(pid)
    watchdog_pids = _pids_for(home, "confluence_knowledge.watchdog")
    wpid = _watchdog_pid(home)
    if wpid is not None:
        watchdog_pids.add(wpid)
    for name, pids in (("daemon", daemon_pids),
                       ("watchdog", watchdog_pids)):
        for pid_ in pids:
            try:
                os.kill(pid_, _TERM)
                out[f"{name}_signalled"] = True
            except (ProcessLookupError, PermissionError, OSError):
                pass
    deadline = time.monotonic() + timeout
    while rpc.daemon_alive() and time.monotonic() < deadline:
        time.sleep(0.2)
    out["daemon_down"] = not rpc.daemon_alive()
    if not out["daemon_down"] and daemon_pids:
        # still breathing after SIGTERM + the full wait — wedge or a
        # suspended loop; SIGKILL is the last word (#253)
        for pid_ in daemon_pids:
            try:
                os.kill(pid_, signal.SIGKILL
                        if hasattr(signal, "SIGKILL") else _TERM)
                out["daemon_killed"] = True
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if out.get("daemon_killed"):
            deadline = time.monotonic() + 3
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
