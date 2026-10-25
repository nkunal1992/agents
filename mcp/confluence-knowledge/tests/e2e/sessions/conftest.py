"""Session-e2e hygiene: these tests spawn REAL daemon + watchdog pairs
under pytest's tmp dirs. A bare `daemon.terminate()` kills only the
direct child — the detached watchdog respawns a fresh daemon and the
pair outlives the test, piling up across local runs (one real run left
72 orphans and exhausted the host thread table). Reap every daemon home
found under tmp_path after each test."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reap_test_daemons(tmp_path):
    yield
    from confluence_knowledge.watchdog import stop_daemon
    for rpc_db in tmp_path.rglob("rpc.db"):
        try:
            stop_daemon(rpc_db.parent, timeout=4)
        except Exception:   # noqa: BLE001,S110 — teardown is best-effort
            pass
