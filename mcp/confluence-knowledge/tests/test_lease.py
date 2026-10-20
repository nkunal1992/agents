"""Cross-process crawl lease — exactly one worker per job, takeover of
stale or dead owners, owner-guarded release."""

import os
import subprocess
import sys
import time

from confluence_knowledge.index import Index
from confluence_knowledge.jobs import LeaseOwner


def test_acquire_then_second_owner_fails(tmp_path):
    idx = Index(tmp_path / "s.db")
    assert idx.acquire_lease("j1", "h1:111:a", pid=111)
    assert not idx.acquire_lease("j1", "h1:222:b", pid=222)


def test_same_owner_reacquires(tmp_path):
    idx = Index(tmp_path / "s.db")
    assert idx.acquire_lease("j1", "h1:111:a", pid=111)
    assert idx.acquire_lease("j1", "h1:111:a", pid=111)  # idempotent


def test_stale_heartbeat_taken_over(tmp_path):
    idx = Index(tmp_path / "s.db")
    assert idx.acquire_lease("j1", "h1:999:a", pid=999)
    # age the heartbeat past the stale window
    with idx.lock:
        idx.db.execute("UPDATE job_lease SET heartbeat=? WHERE job_id=?",
                       (time.time() - 60, "j1"))
        idx.db.commit()
    assert idx.acquire_lease("j1", "h1:222:b", pid=222, stale_s=30)


def test_dead_pid_same_host_taken_over(tmp_path):
    idx = Index(tmp_path / "s.db")
    import socket
    host = socket.gethostname()
    # spawn + reap a pid — guaranteed dead by the time we check
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = proc.pid
    proc.wait()
    assert idx.acquire_lease(
        "j1", f"{host}:{dead_pid}:a", pid=dead_pid)
    # live owner must NOT be taken over even on same host
    live = os.getpid()
    assert idx.acquire_lease("j2", f"{host}:{live}:a", pid=live)
    assert not idx.acquire_lease("j2", f"{host}:{dead_pid}:b", pid=dead_pid)


def test_release_is_owner_guarded(tmp_path):
    idx = Index(tmp_path / "s.db")
    assert idx.acquire_lease("j1", "h1:111:a", pid=111)
    idx.release_lease("j1", "h1:222:b")      # not ours — no-op
    with idx.lock:
        assert idx.db.execute(
            "SELECT 1 FROM job_lease WHERE job_id='j1'").fetchone()
    idx.release_lease("j1", "h1:111:a")
    with idx.lock:
        assert idx.db.execute(
            "SELECT 1 FROM job_lease WHERE job_id='j1'").fetchone() is None


def test_heartbeat_returns_state_and_updates(tmp_path):
    idx = Index(tmp_path / "s.db")
    idx.create_job("j1", "1")
    idx.save_job("j1", "running", ["page:1"], [])
    assert idx.acquire_lease("j1", "h1:111:a", pid=111)
    assert idx.heartbeat("j1", "h1:111:a") == "running"
    idx.save_job("j1", "cancelled", [], [])
    assert idx.heartbeat("j1", "h1:111:a") == "cancelled"
    # non-owner heartbeat is refused
    assert idx.heartbeat("j1", "h1:222:b") is None


def test_two_indexes_one_db_one_winner(tmp_path):
    """Two Index connections on one file simulate two IDE windows — only
    one may own the worker."""
    a, b = Index(tmp_path / "s.db"), Index(tmp_path / "s.db")
    assert a.acquire_lease("j1", "h1:111:a", pid=111)
    assert not b.acquire_lease("j1", "h1:222:b", pid=222)
    a.release_lease("j1", "h1:111:a")
    assert b.acquire_lease("j1", "h1:222:b", pid=222)


def test_lease_owner_shape():
    o = LeaseOwner()
    host, pid, nonce = o.value.rsplit(":", 2)
    assert host == o.host and int(pid) == o.pid and len(nonce) == 6
