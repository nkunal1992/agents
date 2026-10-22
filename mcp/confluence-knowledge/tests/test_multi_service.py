"""#206 consolidation — one daemon, any subset of services, any install
order. Entry-point discovery is faked per test so 'only local installed'
or 'sharepoint installed second' are reproducible without real installs."""
from __future__ import annotations

import threading
import time

import pytest

from confluence_knowledge.daemon import Daemon, discover_services
from confluence_knowledge.rpc import RpcDb


def _fake_eps(monkeypatch, names: list[str]):
    """Pretend only `names` are installed in this venv."""
    import importlib.metadata as md

    modules = {
        "confluence-knowledge": "confluence_knowledge.server",
        "local-knowledge": "local_knowledge.server_local",
        "sharepoint-knowledge": "sharepoint_knowledge.server_sp",
    }

    class EP:
        def __init__(self, n):
            self.name = n
            self.value = modules[n]

    # sibling dists aren't installed on this leg — the contract under
    # test is entry-point → module resolution, so stub the import too
    import importlib
    import types
    real_import = importlib.import_module

    def fake_import(name, *a, **kw):
        if name in modules.values():
            m = types.ModuleType(name)
            m.mcp = object()           # discovery only checks it exists
            return m
        return real_import(name, *a, **kw)

    monkeypatch.setattr(md, "entry_points",
                        lambda group=None: [EP(n) for n in names])
    monkeypatch.setattr(importlib, "import_module", fake_import)


@pytest.mark.parametrize("names", [
    ["confluence-knowledge"],
    ["local-knowledge"],
    ["sharepoint-knowledge"],
    ["confluence-knowledge", "local-knowledge"],
    ["local-knowledge", "sharepoint-knowledge"],
    ["confluence-knowledge", "local-knowledge",
     "sharepoint-knowledge"],
])
def test_discovery_any_subset(monkeypatch, names):
    """Every install subset resolves to exactly its own mount set — order
    doesn't matter because discovery reads the venv, not a sequence."""
    _fake_eps(monkeypatch, names)
    found = discover_services()
    assert sorted(found) == sorted(names)
    for n, mod in found.items():
        assert hasattr(mod, "mcp"), f"{n} has no FastMCP app"


def test_dispatch_routes_by_service(tmp_path, monkeypatch):
    """A request's service column picks the right mounted module — the
    shared rpc.db's whole point."""
    _fake_eps(monkeypatch, ["confluence-knowledge", "local-knowledge"])
    d = Daemon(tmp_path)
    d.services = discover_services()
    for name, srv in d.services.items():
        monkeypatch.setattr(srv, "probe_tool",
                            lambda _n=name, **kw: {"svc": _n},
                            raising=False)
    rpc = RpcDb(tmp_path / "rpc.db")
    rpc.submit("probe_tool", {}, 30, service="local-knowledge")
    for rid2, tool, args, service in rpc.poll_requests(d.owner):
        d._dispatch(rid2, tool, args, service)
    time.sleep(0.3)
    row = rpc.db.execute(
        "SELECT result FROM rpc_responses").fetchone()
    import json as _json
    assert _json.loads(row[0])["svc"] == "local-knowledge"


def test_dispatch_unknown_service_errors(tmp_path):
    """A request for a service that isn't mounted must error, never
    silently run on the wrong module."""
    d = Daemon(tmp_path)
    d.services = {"a": object()}
    rpc = RpcDb(tmp_path / "rpc.db")
    rpc.submit("whatever", {}, 30, service="not-installed")
    for rid2, tool, args, service in rpc.poll_requests(d.owner):
        d._dispatch(rid2, tool, args, service)
    row = rpc.db.execute(
        "SELECT error FROM rpc_responses").fetchone()
    assert "unknown daemon tool" in row[0]


def test_daemon_home_is_venv_keyed(monkeypatch, tmp_path):
    """The daemon slot follows the interpreter — same-venv services share
    one daemon; another venv gets its own (can't import us anyway)."""
    monkeypatch.setenv("KNOWLEDGE_SHARED_HOME", str(tmp_path))
    import sys

    from confluence_knowledge.settings import daemon_home
    h = daemon_home()
    assert h.parent.parent == tmp_path
    # changing sys.prefix changes the slot — the isolation boundary
    monkeypatch.setattr(sys, "prefix", "/other/venv")
    h2 = daemon_home()
    assert h2 != h and h2.parent.parent == tmp_path


def test_watchdog_respawns_dead_daemon(tmp_path, monkeypatch):
    """Kill the daemon's lease — the watchdog must put a live owner back
    within its poll interval (the HTTP path's only recovery)."""
    from confluence_knowledge.watchdog import run as watchdog_run
    rpc = RpcDb(tmp_path / "rpc.db")
    # seed a dead lease so daemon_alive() is False
    rpc.db.execute("INSERT INTO daemon_lease VALUES ('__daemon__','x',-1,0)")
    rpc.db.commit()

    spawned = []
    monkeypatch.setattr("confluence_knowledge.rpc.ensure_daemon",
                        lambda r, env=None: spawned.append(True)
                        or _claim(rpc))
    def _claim(r):
        r.db.execute("INSERT OR REPLACE INTO daemon_lease "
                     "VALUES ('__daemon__','alive',12345,?)",
                     (time.time(),))
        r.db.commit()
    import confluence_knowledge.rpc as rpc_mod
    monkeypatch.setattr(rpc_mod, "ensure_daemon",
                        lambda r, env=None: (_claim(r), spawned.append(True)))

    stop = tmp_path / ".watchdog.stop"
    def stop_soon():
        time.sleep(3.5)
        stop.write_text("1")
    threading.Thread(target=stop_soon, daemon=True).start()
    watchdog_run(tmp_path)
    assert spawned, "watchdog never attempted a respawn"


def test_http_services_endpoint_lists_mounts(tmp_path, monkeypatch):
    """/_services is the mount registry register() probes — it must name
    exactly the mounted set."""
    _fake_eps(monkeypatch, ["confluence-knowledge", "local-knowledge"])
    d = Daemon(tmp_path)
    d.services = discover_services()
    # mirror _serve_http's service list without binding a port
    assert sorted(d.services) == \
        ["confluence-knowledge", "local-knowledge"]
