"""#239 respawn-poll helpers shared by the daemon-kill session tests.

The flake: on congested CI runners the fixed deadline elapsed while the
respawn was still legitimately booting, and the bare "watchdog never
respawned" assertion carried zero diagnostics — the daemon.log tail had
to be dug out of a separate artifact. Two improvements:

- `respawn_deadline` — env-tunable (`KNOWLEDGE_E2E_RESPAWN_S`) so a
  pathological runner can be accommodated without a code change.
- `watchdog_failure` — early, provable death detection. If the process
  named by `.watchdog.pid` is dead there is nothing left to respawn the
  daemon: polling the full window only burns CI time. Two samples 1s
  apart guard against reading the pid file mid-rewrite.
- `supervision_tail` — embeds daemon.log/watchdog.log tails directly in
  the assertion message.
"""
from __future__ import annotations

import os
import time
from pathlib import Path


def respawn_deadline() -> float:
    return float(os.environ.get("KNOWLEDGE_E2E_RESPAWN_S", "120"))


def supervision_tail(home: Path, lines: int = 30) -> str:
    out = []
    for name in ("daemon.log", "watchdog.log"):
        f = home / name
        if f.exists():
            tail = f.read_text(errors="replace").splitlines()[-lines:]
            out.append(f"---- {name} (last {len(tail)}) ----\n"
                       + "\n".join(tail))
    return "\n".join(out) or "(no daemon.log/watchdog.log under home)"


def _pid_file_watchdog_dead(home: Path) -> bool:
    pid_file = home / ".watchdog.pid"
    if not pid_file.exists():
        return False               # not written yet — can't conclude
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return False               # mid-write — can't conclude
    from confluence_knowledge.rpc import _cmdline_is
    return not _cmdline_is(pid, "confluence_knowledge.watchdog")


def watchdog_failure(home: Path) -> str | None:
    """A reason string when the watchdog named by .watchdog.pid is
    provably dead — confirmed twice 1s apart so a pid file observed
    mid-rewrite can't false-trigger. None while the question is open."""
    if not _pid_file_watchdog_dead(home):
        return None
    time.sleep(1)
    if not _pid_file_watchdog_dead(home):
        return None
    try:
        pid = (home / ".watchdog.pid").read_text(
            encoding="utf-8").strip()
    except OSError:
        pid = "?"
    return f".watchdog.pid {pid} is dead — nothing left to respawn"
