"""#172: the memory-guard pause has a ceiling — an unreachable floor must
not deadlock a crawl."""

import threading
import time

from confluence_knowledge import jobs, server
from confluence_knowledge.index import Index
from confluence_knowledge.jobs import Progress, crawl_control


def _ctl(tmp_path, monkeypatch, pause_max=0.15, cooldown=0.3):
    monkeypatch.setattr(jobs, "_MEM_PAUSE_MAX_S", pause_max)
    monkeypatch.setattr(jobs, "_MEM_PAUSE_COOLDOWN_S", cooldown)
    # pin availability under the danger floor — above it the guard
    # runs constrained instead of pausing (#245)
    monkeypatch.setattr(jobs, "_avail_mb", lambda: 200.0)
    idx = Index(tmp_path / "s.db")
    idx.create_job("j", "1")
    idx.save_job("j", "running", ["page:1"], [])
    idx.acquire_lease("j", server._owner.value, server._owner.pid)
    return crawl_control(idx, "j", Progress(), cancel_flag=None,
                         owner=server._owner, hb_s=60)


def test_pause_yields_after_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "limits",
                        lambda: type("L", (), {"memory_ok": lambda s: False})())
    ctl = _ctl(tmp_path, monkeypatch)
    ctl.sleep = lambda s: time.sleep(0.01)
    t0 = time.monotonic()
    ctl.check()                      # floor unreachable → must not hang
    assert time.monotonic() - t0 < 5
    assert "resuming" in ctl.progress.phase or \
        ctl._pause_override_until > 0


def test_pause_resets_on_recovery(tmp_path, monkeypatch):
    state = {"ok": False}
    monkeypatch.setattr(
        jobs, "limits",
        lambda: type("L", (), {"memory_ok": lambda s: state["ok"]})())
    ctl = _ctl(tmp_path, monkeypatch)
    ctl.sleep = lambda s: (state.update(ok=True), time.sleep(0.001))
    ctl.check()                      # memory recovers mid-pause → exits loop
    assert ctl._paused_since is None
    assert ctl._pause_override_until == 0.0


def test_normal_pause_still_engages(tmp_path, monkeypatch):
    state = {"ok": False}
    monkeypatch.setattr(
        jobs, "limits",
        lambda: type("L", (), {"memory_ok": lambda s: state["ok"]})())
    ctl = _ctl(tmp_path, monkeypatch, pause_max=60)   # ceiling not reached
    ctl.sleep = lambda s: time.sleep(0.01)
    done = threading.Event()
    threading.Thread(target=lambda: (ctl.check(), done.set()),
                     daemon=True).start()
    for _ in range(300):
        if ctl.progress.phase == "paused: low memory":
            break
        time.sleep(0.01)
    assert ctl.progress.phase == "paused: low memory"
    state["ok"] = True                    # recovery → loop exits cleanly
    assert done.wait(5)
