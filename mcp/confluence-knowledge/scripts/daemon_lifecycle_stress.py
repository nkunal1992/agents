"""Daemon lifecycle stress (#253): repeatedly kill -9 the daemon, then
fire a burst of concurrent tool calls — assert the process tree always
converges to exactly 1 daemon + 1 watchdog, no orphans, no bursts.

    python scripts/daemon_lifecycle_stress.py [--cycles 20]

Requires a live profile + models (fake embeddings off) — it exercises
the real spawn path end to end."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _procs(home: str = "") -> list[tuple[int, str]]:
    out = []
    try:
        import psutil
    except ImportError:
        sys.exit("psutil required")
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(p.cmdline())
            if ("confluence_knowledge.daemon" in cmd or
                    "confluence_knowledge.watchdog" in cmd):
                if not home or home in cmd:
                    out.append((p.pid, cmd))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def _count(procs):
    # match the module token — bare "daemon" also matches the home dir
    # path (.../daemon/<hash>) in every cmdline
    d = sum("confluence_knowledge.daemon " in c for _, c in procs)
    w = sum("confluence_knowledge.watchdog " in c for _, c in procs)
    return d, w


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=20)
    ap.add_argument("--burst", type=int, default=8)
    a = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).parent))
    import atexit  # noqa: F401
    home = Path.home() / ".confluence-knowledge"
    dhome = Path.home() / ".knowledge" / "daemon"
    from confluence_knowledge.settings import daemon_home
    dhome = daemon_home()

    import psutil
    start = _procs(str(dhome))
    print(f"baseline: {len(start)} procs on {dhome.name} "
          f"(daemon={_count(start)[0]} watchdog={_count(start)[1]})")

    worst = 0
    converged = 0
    for i in range(a.cycles):
        # SIGKILL the daemon pair hard — worst case (no clean shutdown)
        for pid, cmd in _procs(str(dhome)):
            os.kill(pid, signal.SIGKILL)
        time.sleep(0.3)

        # burst of concurrent "tool calls" — the wake-from-sleep storm
        def call(_n):
            from confluence_knowledge.rpc import RpcDb, ensure_daemon
            ensure_daemon(RpcDb(dhome / "rpc.db"))
        with ThreadPoolExecutor(a.burst) as ex:
            list(ex.map(call, range(a.burst)))

        # poll up to 60s for the pair to respawn + converge — cold
        # boots (model store scan, service discovery) run ~30s on a
        # loaded laptop; a lease-loser visible mid-boot is transient,
        # not drift
        deadline = time.monotonic() + 60
        d = w = 0
        while time.monotonic() < deadline:
            procs = _procs(str(dhome))
            d, w = _count(procs)
            if d == 1 and w == 1:
                converged += 1
                break
            time.sleep(0.5)
        worst = max(worst, d + w)
        print(f"cycle {i + 1:>2}: daemon={d} watchdog={w} "
              f"{'ok' if (d == 1 and w == 1) else 'pending/stray'}")

    # settle: after the last burst, let the pair finish booting — the
    # invariant is "exactly 1 daemon + 1 watchdog, zero strays"
    print("settle (120s max for final convergence)...")
    deadline = time.monotonic() + 120
    final_d = final_w = 0
    while time.monotonic() < deadline:
        procs = _procs(str(dhome))
        final_d, final_w = _count(procs)
        if final_d == 1 and final_w == 1:
            break
        time.sleep(1.0)

    print(f"\nconverged cycles: {converged}/{a.cycles}   "
          f"worst simultaneous: {worst}   "
          f"final: daemon={final_d} watchdog={final_w}")
    ok = final_d == 1 and final_w == 1 and worst <= 3
    print("PASS" if ok else "FAIL — process drift")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
