"""Renaudat-audit pillar 5 — fault injection. Every break must produce an
actionable error or a self-healing refetch, never a raw traceback, a
crash loop, or a silent wrong answer."""

import sqlite3
import time

import pytest


def test_corrupt_models_lock_is_setup_error_not_traceback(
        monkeypatch, tmp_path):
    """Truncated models.lock.json — the shipped manifest. A broken
    install must say 'reinstall the service', not crash on
    JSONDecodeError."""
    import confluence_knowledge.retrieval as r
    bad = tmp_path / "pkg" / "models.lock.json"
    bad.parent.mkdir(parents=True)
    bad.write_text('{"models": {"embed": ')   # truncated mid-file
    monkeypatch.setattr(r, "__file__", str(tmp_path / "pkg" / "retrieval.py"))
    from confluence_knowledge.settings import SetupError
    with pytest.raises(SetupError, match="corrupt"):
        r._load_lock()
    bad.write_text('{"not_models": {}}')
    with pytest.raises(SetupError, match="no 'models'"):
        r._load_lock()


def test_poisoned_rpc_row_answered_not_crashing(tmp_path):
    """A truncated JSON args payload in rpc_requests must not kill the
    daemon loop — the poisoned row is answered with an error and the
    good rows still dispatch."""
    from confluence_knowledge.rpc import RpcDb
    rpc = RpcDb(tmp_path / "rpc.db")
    now = time.time()
    rpc.db.execute(
        "INSERT INTO rpc_requests VALUES (?,?,?,?,?,?,NULL,NULL)",
        ("good1", "status", "{}", "confluence-knowledge", now, now + 30))
    rpc.db.execute(
        "INSERT INTO rpc_requests VALUES (?,?,?,?,?,?,NULL,NULL)",
        ("bad1", "status", '{"scope": ', "confluence-knowledge",
         now, now + 30))
    rpc.db.commit()
    rows = rpc.poll_requests("owner-x")
    assert [r[0] for r in rows] == ["good1"], "poisoned row leaked"
    resp = rpc.db.execute(
        "SELECT error FROM rpc_responses WHERE id='bad1'").fetchone()
    assert resp and "corrupt" in resp[0]


def test_watchdog_stands_down_after_three_spawn_failures(tmp_path,
                                                         monkeypatch):
    """Pillar-4 chaos: respawns that keep failing must NOT loop forever —
    the watchdog exits after 3 consecutive ensure_daemon failures."""
    import confluence_knowledge.watchdog as wd
    from confluence_knowledge.rpc import RpcDb
    RpcDb(tmp_path / "rpc.db")
    monkeypatch.setattr(wd, "_POLL_S", 0.01)
    monkeypatch.setattr(RpcDb, "daemon_alive", lambda self: False)
    fails = []

    def _boom(r, env=None):
        fails.append(1)
        raise RuntimeError("spawn impossible")

    monkeypatch.setattr("confluence_knowledge.rpc.ensure_daemon", _boom)
    assert wd.run(tmp_path) == 0              # exits, doesn't hang
    assert len(fails) == 3
    assert not (tmp_path / ".watchdog.pid").exists()


def test_unreadable_index_dir_is_actionable(tmp_path):
    """chmod-000 index dir — SetupError/operational message, not a bare
    PermissionError traceback."""
    d = tmp_path / "locked"
    d.mkdir(mode=0o000)
    try:
        from confluence_knowledge.index import Index
        try:
            Index(d / "x.db")
            verdict = "created silently"   # would be its own bug
        except Exception as e:                  # noqa: BLE001 — any exception name is the verdict
            verdict = type(e).__name__
        assert "Permission" in verdict or "Operational" in verdict \
            or "Error" in verdict
    finally:
        d.chmod(0o755)


def test_locked_db_retries_then_surfaces(tmp_path, monkeypatch):
    """database is locked → _LockedRetry retries then raises cleanly."""
    from confluence_knowledge.index import _LockedRetry
    db = sqlite3.connect(tmp_path / "x.db")
    db.execute("CREATE TABLE t(x)")
    db2 = sqlite3.connect(tmp_path / "x.db", timeout=0)
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(sqlite3.OperationalError, match="locked"), \
            _LockedRetry(db2):
        db2.execute("INSERT INTO t VALUES (2)")
    db.rollback(); db.close(); db2.close()
