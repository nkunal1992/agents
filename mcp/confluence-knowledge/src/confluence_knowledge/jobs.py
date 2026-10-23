"""Crawl job coordination — lease owner, live progress, control hooks.

The SQLite `job_lease` row is the cross-process fence: exactly one MCP
server process (one IDE window) runs a worker on a given job. Progress is
kept in memory so polls never wait on the index write lock, and is
persisted at checkpoints so other processes can read it too.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field

from .client import AccessError
from .resources import _avail_mb, limits


class CrawlCancelled(Exception):
    """Raised inside the crawler when the job was cancelled."""


class LeaseOwner:
    """This process's identity in `job_lease.owner` — hostname:pid:nonce.
    Inside the daemon (#189) the 'daemon:' prefix lets the proxy label the
    holder honestly as the brain rather than 'another IDE window'."""

    def __init__(self) -> None:
        self.host = socket.gethostname()
        self.pid = os.getpid()
        prefix = "daemon:" if os.environ.get("KNOWLEDGE_DAEMON_ROLE") else ""
        self.value = (f"{prefix}{self.host}:{self.pid}:"
                      f"{uuid.uuid4().hex[:6]}")


@dataclass
class Progress:
    """Lock-free-ish live crawl progress — written by the worker, read by
    polls. single-writer so plain attribute stores are fine."""

    started_at: float = field(default_factory=time.time)
    pages_done: int = 0
    pages_seen: int = 0     # discovered/processed — moves during fetching
    remaining: int = 0
    errors_n: int = 0
    phase: str = "starting"
    rate_ppm: float = 0.0          # EWMA of pages/minute
    worker: str = "self"           # or a lease-holder label
    fetch_cap: int = 0    # current adaptive submission cap (#179)
    battery: bool = False  # durable flag — saver engaged this crawl
    updated_at: float = field(default_factory=time.time)
    _win_n: int = 0
    _win_t: float = field(default_factory=time.time)

    def page_done(self, n: int = 1, at: float | None = None) -> None:
        at = at if at is not None else time.time()
        self.pages_done += n
        self._win_n += n
        if at - self._win_t >= 10:
            inst = self._win_n / max(at - self._win_t, 0.001) * 60
            self.rate_ppm = inst if self.rate_ppm == 0 else \
                0.8 * self.rate_ppm + 0.2 * inst
            self._win_n, self._win_t = 0, at
        self.updated_at = at

    def set_phase(self, name: str) -> None:
        self.phase = name
        self.updated_at = time.time()

    def snapshot(self) -> dict:
        rate = self.rate_ppm
        eta = (int(self.remaining / (rate / 60))
               if rate and self.remaining else None)
        return {"pages_done": self.pages_done, "pages_seen": self.pages_seen,
                "remaining": self.remaining,
                "errors_n": self.errors_n, "phase": self.phase,
                "rate_ppm": (None if not rate else round(rate, 1)),
                "eta_s": eta,
                "worker": self.worker,
                "fetch_cap": self.fetch_cap,
                "battery": self.battery,
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "elapsed_s": max(0.0, round(
                    (self.updated_at or self.started_at)
                    - self.started_at, 1))}

    @classmethod
    def from_blob(cls, blob: str) -> Progress | None:
        try:
            d = json.loads(blob)
            p = cls()
            p.pages_done = int(d.get("pages_done", 0))
            p.pages_seen = int(d.get("pages_seen", 0))
            p.remaining = int(d.get("remaining", 0))
            p.errors_n = int(d.get("errors_n", 0))
            p.phase = str(d.get("phase", "running"))
            p.rate_ppm = float(d.get("rate_ppm") or 0.0)
            p.worker = "another server process"
            p.fetch_cap = int(d.get("fetch_cap", 0))
            p.battery = bool(d.get("battery", False))
            p.started_at = float(d.get("started_at", time.time()))
            p.updated_at = float(d.get("updated_at", time.time()))
            return p
        except Exception:  # noqa: BLE001 — corrupt blob → caller falls back
            return None


# Memory-guard pause ceiling: a miscalibrated KNOWLEDGE_MIN_FREE_MB (or
# baseline drift on a busy machine) can make the floor permanently
# unreachable — a crawl must never deadlock. After this much paused time
# the guard yields for a cool-down, then may re-engage (#172).
_MEM_PAUSE_MAX_S = float(os.environ.get("KNOWLEDGE_MEM_PAUSE_MAX_S", "600"))
_MEM_PAUSE_COOLDOWN_S = float(
    os.environ.get("KNOWLEDGE_MEM_PAUSE_COOLDOWN_S", "60"))


def _mem_danger_mb() -> float:
    """The hard-OOM floor — below this even a minimal-footprint embed is
    unsafe, so the crawl truly pauses. Between `min_free_mb` and this
    floor the crawl runs CONSTRAINED (fetch window 1, smallest embed
    batches — live limits() already shrink them) instead of freezing
    for 10-minute cycles (#245)."""
    raw = os.environ.get("KNOWLEDGE_MEM_DANGER_MB", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return max(250.0, getattr(limits(), "min_free_mb", 1500) * 0.2)

# Auth lifecycle (#177): an expired token pauses the job rather than
# burning errors; the crawler probes for a rotated credential and resumes
# in place. A ceiling prevents an unattended pause from hanging forever.
_AUTH_PROBE_S = float(os.environ.get("KNOWLEDGE_AUTH_PROBE_S", "10"))
_AUTH_PAUSE_MAX_S = float(os.environ.get("KNOWLEDGE_AUTH_PAUSE_MAX_S", "1800"))

# Adaptive fetch window (#179): the submission cap breathes with system
# load — scaled down hard when the laptop is busy, allowed toward
# KNOWLEDGE_FETCH_MAX when idle (0 = capped at the base, fully inert).
# The pool is sized at the ceiling once per step; only the submission
# gate moves, so there's no worker churn mid-crawl.
_CPU_SAMPLE_S = 2.0


def _cpu_percent() -> float:
    """System-wide CPU utilisation since the last call — psutil's
    interval=None form never blocks. Absent psutil → mid-band (base)."""
    try:
        import psutil
        return psutil.cpu_percent(interval=None)
    except Exception:  # noqa: BLE001 — no sensor → no adaptation
        return 50.0


def _fetch_ceiling(base: int) -> int:
    """Adaptive ceiling — `KNOWLEDGE_FETCH_MAX` (0 → = base)."""
    v = int(os.environ.get("KNOWLEDGE_FETCH_MAX", "0") or 0)
    return min(16, max(base, v)) if v > 0 else base


class CrawlControl:
    """Per-job hook the crawler consults between queue items and embed
    flushes: cancellation, memory guard, lease heartbeat, phase, rate."""

    def __init__(self, idx, job_id: str, progress: Progress,
                 cancel_flag: threading.Event | None = None,
                 owner: LeaseOwner | None = None,
                 sleep=time.sleep, hb_s: float = 5.0,
                 notify=None) -> None:
        self.idx = idx
        self.job_id = job_id
        self.progress = progress
        self.cancel_flag = cancel_flag
        self.owner = owner
        self.sleep = sleep
        self.hb_s = hb_s             # heartbeat/cross-process-cancel cadence
        self.notify = notify         # (title, body) desktop hook or None
        self._last_hb = 0.0
        self._battery_checked = 0.0
        self._battery = False
        self._cpu_checked = 0.0
        self._cpu = 50.0
        self._fetch_cap = 0
        self._paused_since: float | None = None
        self._pause_override_until = 0.0
        self._mem_constrained = False

    def check(self) -> None:
        """Call between queue items and embed flushes. Order: cancel →
        heartbeat (also detects cross-process cancel/fence) → memory guard."""
        if self.cancel_flag is not None and self.cancel_flag.is_set():
            raise CrawlCancelled()
        self._heartbeat()
        if time.time() < self._pause_override_until:
            return
        if limits().memory_ok():
            self._mem_constrained = False
        while not limits().memory_ok():
            avail = _avail_mb()
            if avail >= _mem_danger_mb():
                # strained but above the danger floor — keep working at
                # minimum footprint (fetch window clamps in
                # fetch_concurrency; embed batches already shrink via
                # live limits()) instead of a 10-min/60s duty cycle
                # (#245)
                if not self._mem_constrained:
                    self._mem_constrained = True
                    self.progress.set_phase(
                        f"constrained: low memory (~{avail:.0f} MB free)")
                    self._flush_progress()
                    logging.getLogger(__name__).info(
                        "memory guard constrained job %s (%.0f MB free)",
                        self.job_id, avail)
                break
            self._mem_constrained = False
            if self._paused_since is None:
                self._paused_since = time.time()
                logging.getLogger(__name__).info(
                    "memory guard paused job %s", self.job_id)
            elif time.time() - self._paused_since >= _MEM_PAUSE_MAX_S:
                # the floor is unreachable (baseline drift or a miscalibrated
                # KNOWLEDGE_MIN_FREE_MB) — never deadlock: resume for a
                # cool-down window, then let the guard re-engage (#172)
                self.progress.set_phase(
                    "resuming: memory floor unreachable")
                self._paused_since = None
                self._pause_override_until = \
                    time.time() + _MEM_PAUSE_COOLDOWN_S
                return
            self.progress.set_phase("paused: low memory")
            # paused workers don't checkpoint — persist the phase so
            # proxies/other windows see 'paused', not a stale phase
            self._flush_progress()
            if self.cancel_flag is not None and self.cancel_flag.is_set():
                raise CrawlCancelled()
            self.sleep(2)
            self._heartbeat()
        if self._paused_since is not None:
            logging.getLogger(__name__).info(
                "memory guard released job %s after %.1f s",
                self.job_id, time.time() - self._paused_since)
        self._paused_since = None

    def _flush_progress(self) -> None:
        """Persist the progress blob NOW (outside the checkpoint cadence)
        — for states that must be visible cross-process immediately."""
        try:
            self.idx.meta_set(f"progress:{self.job_id}",
                              json.dumps(self.progress.snapshot()))
        except Exception as e:  # noqa: BLE001 — never crash a crawl on this
            logging.getLogger(__name__).debug("progress flush failed: %s",
                                              e)

    def pause_until_auth(self, probe) -> None:
        """Auth expiry is a job-class pause, not page errors (#177): the
        job waits for a token rotation, then resumes from exactly where
        it stopped. `probe()` → True when credentials re-validate. A
        ceiling keeps an unattended expiry from hanging forever."""
        self.progress.set_phase(
            "paused: auth — session expired, provide a new API token "
            "to continue")
        # paused workers don't checkpoint — persist the phase so
        # proxies/other windows see 'session expired', not a stale
        # phase (same pattern as the memory pause above)
        self._flush_progress()
        if self.notify is not None:
            try:
                self.notify(
                    "confluence-knowledge",
                    "session expired — provide a new API token to "
                    "continue the crawl")
            except Exception:  # noqa: BLE001,S110 — notify is best-effort
                pass
        since = time.time()
        while True:
            if self.cancel_flag is not None and self.cancel_flag.is_set():
                raise CrawlCancelled()
            if time.time() - since >= _AUTH_PAUSE_MAX_S:
                raise AccessError(
                    "auth pause timed out — no new credential arrived "
                    f"within {_AUTH_PAUSE_MAX_S:.0f}s; re-run start_crawl "
                    "after setup")
            try:
                if probe():
                    self.progress.set_phase("resuming: token rotated")
                    self._flush_progress()
                    return
            except Exception:  # noqa: BLE001,S110 — probe errors = still dead
                pass
            self.sleep(_AUTH_PROBE_S)
            self._heartbeat()

    def _heartbeat(self) -> None:
        now = time.time()
        if now - self._last_hb < self.hb_s or self.owner is None:
            return
        self._last_hb = now
        state = self.idx.heartbeat(self.job_id, self.owner.value)
        if state is not None and state != "running":
            raise CrawlCancelled()

    def phase(self, name: str) -> None:
        self.progress.set_phase(name)

    def page_done(self, n: int = 1) -> None:
        self.progress.page_done(n)

    def fetch_ceiling(self, default: int) -> int:
        """Pool size for a step — the largest cap fetch_concurrency()
        could return, so the executor never needs resizing (#179)."""
        return _fetch_ceiling(default)

    def fetch_concurrency(self, default: int) -> int:
        """Current submission cap — CPU-pressure ladder with a battery
        floor; scale-up only ever reaches the env ceiling (#179)."""
        now = time.time()
        if now - self._battery_checked >= 60:
            self._battery_checked = now
            self._battery = limits().on_battery()
        if now - self._cpu_checked >= _CPU_SAMPLE_S:
            self._cpu_checked = now
            self._cpu = _cpu_percent()
        cpu = self._cpu
        if cpu >= 90:
            cur = 1
        elif cpu >= 75:
            cur = max(1, default // 2)
        elif cpu < 40:
            cur = min(self.fetch_ceiling(default),
                      max(default, int(default * 1.5)))
        else:
            cur = default
        if self._battery:
            cur = min(cur, max(1, default // 2))
        if self._mem_constrained:
            cur = min(cur, 2)          # #245 — trickle, don't freeze
        self._fetch_cap = cur
        return cur

    def battery_saver(self) -> bool:
        self.fetch_concurrency(1)      # refresh the cached flag
        if self._battery:
            self.progress.battery = True    # durable flag for the blob
        return self._battery


def crawl_control(idx, job_id: str, progress: Progress,
                  cancel_flag: threading.Event | None = None,
                  owner: LeaseOwner | None = None,
                  hb_s: float = 5.0, notify=None) -> CrawlControl:
    """Server-side factory — crawlers accept control=None (tests/CLI get a
    no-op), workers get a real one."""
    return CrawlControl(idx, job_id, progress, cancel_flag, owner,
                        hb_s=hb_s, notify=notify)
