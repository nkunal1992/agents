"""Crawl job coordination — lease owner, live progress, control hooks.

The SQLite `job_lease` row is the cross-process fence: exactly one MCP
server process (one IDE window) runs a worker on a given job. Progress is
kept in memory so polls never wait on the index write lock, and is
persisted at checkpoints so other processes can read it too.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field

from .resources import limits


class CrawlCancelled(Exception):
    """Raised inside the crawler when the job was cancelled."""


class LeaseOwner:
    """This process's identity in `job_lease.owner` — hostname:pid:nonce."""

    def __init__(self) -> None:
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self.value = f"{self.host}:{self.pid}:{uuid.uuid4().hex[:6]}"


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
    worker: str = "self"           # or "another IDE window"
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
            p.worker = "another IDE window"
            p.started_at = float(d.get("started_at", time.time()))
            p.updated_at = float(d.get("updated_at", time.time()))
            return p
        except Exception:  # noqa: BLE001 — corrupt blob → caller falls back
            return None


class CrawlControl:
    """Per-job hook the crawler consults between queue items and embed
    flushes: cancellation, memory guard, lease heartbeat, phase, rate."""

    def __init__(self, idx, job_id: str, progress: Progress,
                 cancel_flag: threading.Event | None = None,
                 owner: LeaseOwner | None = None,
                 sleep=time.sleep, hb_s: float = 5.0) -> None:
        self.idx = idx
        self.job_id = job_id
        self.progress = progress
        self.cancel_flag = cancel_flag
        self.owner = owner
        self.sleep = sleep
        self.hb_s = hb_s             # heartbeat/cross-process-cancel cadence
        self._last_hb = 0.0
        self._battery_checked = 0.0
        self._battery = False

    def check(self) -> None:
        """Call between queue items and embed flushes. Order: cancel →
        heartbeat (also detects cross-process cancel/fence) → memory guard."""
        if self.cancel_flag is not None and self.cancel_flag.is_set():
            raise CrawlCancelled()
        self._heartbeat()
        while not limits().memory_ok():
            self.progress.set_phase("paused: low memory")
            if self.cancel_flag is not None and self.cancel_flag.is_set():
                raise CrawlCancelled()
            self.sleep(2)
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

    def fetch_concurrency(self, default: int) -> int:
        now = time.time()
        if now - self._battery_checked >= 60:
            self._battery_checked = now
            self._battery = limits().on_battery()
        if self._battery:
            return max(1, default // 2)
        return default

    def battery_saver(self) -> bool:
        self.fetch_concurrency(1)      # refresh the cached flag
        return self._battery


def crawl_control(idx, job_id: str, progress: Progress,
                  cancel_flag: threading.Event | None = None,
                  owner: LeaseOwner | None = None,
                  hb_s: float = 5.0) -> CrawlControl:
    """Server-side factory — crawlers accept control=None (tests/CLI get a
    no-op), workers get a real one."""
    return CrawlControl(idx, job_id, progress, cancel_flag, owner,
                        hb_s=hb_s)
