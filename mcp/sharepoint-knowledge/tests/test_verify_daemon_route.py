"""Evidence sets are process-local — under daemon mode `search`
populates the DAEMON's registry, so verify must route there first."""

import os as _os

import pytest as _pt

# Process-lifecycle tests are POSIX-signal-centric (daemon spawn, lease
# recovery, SIGKILL respawn). On Windows runners the spawned daemon
# shares pytest's console group — the unit leg dies on a stray console
# interrupt ~6s into the first daemon test (reproducible on 3.11+3.13).
# Windows daemon coverage lives in tests/e2e/sessions instead.
pytestmark = _pt.mark.skipif(_os.name == "nt",
                             reason="daemon lifecycle is POSIX-centric; "
                                    "Windows coverage is in e2e sessions")
import sharepoint_knowledge.server_sp as srv


def test_verify_citations_routes_to_daemon(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        srv, "_daemon_call",
        lambda name, args, timeout: (
            seen.update(name=name) or {"ok": True}))
    out = srv.verify_citations("scope:x", "set:1", [])
    assert seen["name"] == "verify_citations" and out == {"ok": True}
