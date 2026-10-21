"""Adaptive throttling — memory guard pauses/resumes embedding, battery
saver halves concurrency and inserts the duty-cycle sleep, missing
psutil disables both."""

import threading
import time

import numpy as np
import pytest
from test_parallel_crawl import _fan_client

import confluence_knowledge.jobs as jobs_mod
from confluence_knowledge.crawl import Crawler
from confluence_knowledge.index import Index
from confluence_knowledge.jobs import CrawlCancelled, CrawlControl, Progress
from confluence_knowledge.resources import limits


def _ctl(idx, job="j1", flag=None):
    return CrawlControl(idx, job, Progress(), cancel_flag=flag)


def test_memory_guard_pauses_and_resumes(tmp_path, monkeypatch):
    """memory_ok False → the control check blocks in 'paused: low memory'
    until it recovers — no embed calls while paused."""
    idx = Index(tmp_path / "s.db")
    idx.create_job("j1", "1")
    idx.save_job("j1", "running", ["page:1"], [])
    ctl = _ctl(idx)
    monkeypatch.setattr(
        type(limits()), "memory_ok", lambda self: False)
    def checker():
        while not ctl.progress.phase.startswith("paused"):
            time.sleep(0.02)
        monkeypatch.setattr(
            type(limits()), "memory_ok", lambda self: True)
    t = threading.Thread(target=checker)
    t.start()
    ctl.check()          # must block until memory recovers
    t.join()
    assert ctl.progress.phase == "paused: low memory"


def test_memory_guard_cancel_wins(tmp_path, monkeypatch):
    """While paused for memory, a cancel still breaks the wait."""
    idx = Index(tmp_path / "s.db")
    flag = threading.Event()
    ctl = _ctl(idx, flag=flag)
    monkeypatch.setattr(
        type(limits()), "memory_ok", lambda self: False)
    threading.Timer(0.3, flag.set).start()
    with pytest.raises(CrawlCancelled):
        ctl.check()


def test_battery_halves_fetch_concurrency(tmp_path, monkeypatch):
    idx = Index(tmp_path / "s.db")
    ctl = _ctl(idx)
    # pin the CPU ladder to the neutral band — on a loaded CI runner
    # cpu>=75 clamps cur to default//2, the same value the battery
    # clamp produces, so the battery halves are indistinguishable
    # without controlling the signal under test.
    monkeypatch.setattr(jobs_mod, "_cpu_percent", lambda: 50.0)
    ctl._cpu_checked = 0.0              # force re-sample → pinned 50
    monkeypatch.setattr(
        type(limits()), "on_battery", lambda self: True)
    assert ctl.fetch_concurrency(8) == 4
    assert ctl.battery_saver() is True
    monkeypatch.setattr(
        type(limits()), "on_battery", lambda self: False)
    ctl._battery_checked = 0.0          # force re-check
    ctl._cpu_checked = 0.0              # CPU could re-sample mid-assert
    assert ctl.fetch_concurrency(8) == 8
    assert ctl.battery_saver() is False


def test_embed_duty_cycle_on_battery(tmp_path, monkeypatch):
    """On battery a flush sleeps roughly its own embed time after each
    batch — a busy-wait embed makes the duty-cycle sleep measurable."""
    monkeypatch.setattr(
        type(limits()), "on_battery", lambda self: True)
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    class BusyEmbed:
        def embed(self, texts):
            end = time.monotonic() + 0.15   # real busy-wait — real elapsed
            while time.monotonic() < end:
                pass
            return np.ones((len(texts), 4), dtype=np.float32)

    idx = Index(tmp_path / "s.db")
    idx.create_job("j1", "1")
    idx.save_job("j1", "running", ["page:1"], [])
    ctl = _ctl(idx)
    c = Crawler(_fan_client(delay=0.0), idx, "datacenter",
                embedder=BusyEmbed(), control=ctl)
    r = c.step("j1", batch=None)
    assert r["state"] == "done"
    # duty-cycle sleep(s) recorded — battery saver slowed the flush path
    assert any(s > 0.01 for s in sleeps), \
        f"no duty-cycle sleep recorded on battery: {sleeps}"


def test_no_psutil_means_no_guard(tmp_path, monkeypatch):
    """Without psutil both adaptations are no-ops."""
    import confluence_knowledge.resources as res
    monkeypatch.setattr(res, "_psutil", None)
    lim = limits()
    assert lim.memory_ok() is True
    assert lim.on_battery() is False


def test_progress_rate_and_snapshot(tmp_path):
    p = Progress()
    t0 = time.time()
    for i in range(30):
        p.page_done(1, at=t0 + i)        # ~1 page/s over a 30s feed
    snap = p.snapshot()
    assert snap["pages_done"] == 30
    assert snap["rate_ppm"] is not None and snap["rate_ppm"] > 0
    assert snap["elapsed_s"] == 29
    back = Progress.from_blob(__import__("json").dumps(snap))
    assert back is not None and back.pages_done == 30
    assert back.worker == "another server process"
