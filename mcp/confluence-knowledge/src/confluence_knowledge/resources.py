"""Process-wide resource policy for the knowledge MCP servers.

Each IDE window spawns its own copy of every installed knowledge server,
so each process must behave like a respectful background citizen: capped
native threads, bounded inference batches, lowered OS priority, and model
memory released when idle. KNOWLEDGE_* env vars override every bound.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.environ.get(name)
    try:
        v = int(raw) if raw is not None else default
    except ValueError:
        v = default
    return max(lo, min(hi, v))


def _default_threads() -> int:
    # ~a quarter of the cores, capped — the IDE and the OS keep the rest
    return max(1, min(4, (os.cpu_count() or 4) // 4))


@dataclass(frozen=True)
class Limits:
    threads: int            # native threads per inference session
    batch_tokens: int       # attention bound: items × Lmax² per micro-batch
    max_batch: int          # max items per micro-batch
    idle_unload_s: float    # unload models after this much idle time (0=never)
    fetch_concurrency: int  # concurrent HTTP fetches during a crawl
    min_free_mb: int        # pause embedding below this much free RAM
    battery_saver: bool     # halve fetch + embed duty-cycle on battery

    def memory_ok(self) -> bool:
        """Enough free RAM that embedding won't push the box into swap —
        swap storms are what actually make a laptop unusable."""
        if _psutil is None:
            return True
        try:
            return _psutil.virtual_memory().available >= \
                self.min_free_mb * 1024 * 1024
        except Exception:  # noqa: BLE001 — can't measure → don't block work
            return True

    def on_battery(self) -> bool:
        # KNOWLEDGE_FORCE_BATTERY=1 is a test/stress-harness hook for
        # machines with no battery sensor or a plugged-in laptop.
        if not self.battery_saver or _psutil is None:
            return False
        forced = os.environ.get("KNOWLEDGE_FORCE_BATTERY")
        if forced is not None:          # "1"=battery, "0"=mains (stress hook)
            return forced == "1"
        try:
            b = _psutil.sensors_battery()
            return b is not None and b.power_plugged is False
        except Exception:  # noqa: BLE001 — desktops have no battery sensor
            return False


try:
    import psutil as _psutil
except ImportError:  # pragma: no cover — dep is pinned, guard anyway
    _psutil = None


def _min_free_mb() -> int:
    """Memory floor for the crawl guard. `KNOWLEDGE_MIN_FREE_MB` accepts
    an absolute MB ("1500") or a percent of total RAM ("10%") — the
    percent form scales to the machine (kubelet/ES watermark style)."""
    raw = os.environ.get("KNOWLEDGE_MIN_FREE_MB", "").strip()
    if raw.endswith("%") and _psutil is not None:
        try:
            pct = float(raw[:-1])
            if 0 < pct < 100:
                return int(_psutil.virtual_memory().total
                           * pct / 100 / 1048576)
        except (ValueError, AttributeError):
            pass
    return _int_env("KNOWLEDGE_MIN_FREE_MB", 1500, 0, 1_000_000)


def limits() -> Limits:
    """Current limits — read live so tests can monkeypatch env per call."""
    return Limits(
        threads=_int_env("KNOWLEDGE_THREADS", _default_threads(), 1, 32),
        batch_tokens=_int_env("KNOWLEDGE_BATCH_TOKENS", 2_500_000,
                              100_000, 64_000_000),
        max_batch=_int_env("KNOWLEDGE_MAX_BATCH", 4, 1, 256),
        idle_unload_s=float(
            _int_env("KNOWLEDGE_IDLE_UNLOAD_S", 600, 0, 86_400)),
        fetch_concurrency=_int_env("KNOWLEDGE_FETCH_CONCURRENCY", 6, 1, 16),
        min_free_mb=_min_free_mb(),
        battery_saver=os.environ.get("KNOWLEDGE_BATTERY_SAVER", "1") != "0",
    )


def lower_priority() -> str:
    """Drop the process's scheduling priority so crawls can't starve the
    IDE/OS. nice +10 on POSIX, BELOW_NORMAL_PRIORITY_CLASS on Windows.
    KNOWLEDGE_NO_NICE=1 opts out. Returns a short human-readable result."""
    if os.environ.get("KNOWLEDGE_NO_NICE") == "1":
        return "priority: unchanged (KNOWLEDGE_NO_NICE)"
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            if k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000):
                return "priority: below-normal"
            return "priority: unchanged (SetPriorityClass failed)"
        os.nice(10)  # noqa: B102 — own process only
        return "priority: nice +10"
    except (OSError, AttributeError):
        return "priority: unchanged (priority lowering unavailable)"


def plan_batches(lengths: list[int], budget: int,
                 max_batch: int) -> list[list[int]]:
    """Partition item indices into micro-batches bounded by attention
    cost (len(batch) × longest²) and item count.

    Transformer attention memory scales with B×L² — measured on this
    codebase: a 3×2048-token run peaks ~+2.5 GB RSS, while 12×450 runs
    peak ~+0.9 GB. A padded-token budget (B×L) still lets several
    full-length items share a batch and spike memory by GBs, so the
    bound is on the square. Sorting by length first keeps outliers in
    tiny batches instead of padding a whole batch up to them. An item
    too big for any budget gets a singleton batch (it can never be
    smaller anyway).
    """
    order = sorted(range(len(lengths)), key=lambda i: -lengths[i])
    batches: list[list[int]] = []
    cur: list[int] = []
    cur_max = 0
    for i in order:
        new_max = max(cur_max, lengths[i])
        if cur and ((len(cur) + 1) * new_max * new_max > budget
                    or len(cur) >= max_batch):
            batches.append(cur)
            cur, cur_max, new_max = [], 0, lengths[i]
        cur.append(i)
        cur_max = new_max
    if cur:
        batches.append(cur)
    return batches
