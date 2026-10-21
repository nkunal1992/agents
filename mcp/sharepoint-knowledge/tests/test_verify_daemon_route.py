"""Evidence sets are process-local — under daemon mode `search`
populates the DAEMON's registry, so verify must route there first."""
import sharepoint_knowledge.server_sp as srv


def test_verify_citations_routes_to_daemon(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        srv, "_daemon_call",
        lambda name, args, timeout: (
            seen.update(name=name) or {"ok": True}))
    out = srv.verify_citations("scope:x", "set:1", [])
    assert seen["name"] == "verify_citations" and out == {"ok": True}
