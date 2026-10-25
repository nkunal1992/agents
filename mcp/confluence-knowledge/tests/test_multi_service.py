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


def test_dispatch_unknown_service_errors(tmp_path, monkeypatch):
    """A request for a service that isn't mounted must error, never
    silently run on the wrong module."""
    _fake_eps(monkeypatch, ["confluence-knowledge"])
    d = Daemon(tmp_path)
    d.services = {"a": object()}
    rpc = RpcDb(tmp_path / "rpc.db")
    rpc.submit("whatever", {}, 30, service="not-installed")
    for rid2, tool, args, service in rpc.poll_requests(d.owner):
        d._dispatch(rid2, tool, args, service)
    row = rpc.db.execute(
        "SELECT error FROM rpc_responses").fetchone()
    assert "not mounted" in row[0]


def test_tagged_miss_never_runs_wrong_service(tmp_path, monkeypatch):
    """#264: a tagged miss must error even when the only mounted service
    HAS a tool of that name — the legacy single-mount fallback once ran
    a local-knowledge connect on confluence's handler and returned a
    nonsense 'numeric page ID' error to the user."""
    _fake_eps(monkeypatch, ["confluence-knowledge"])
    d = Daemon(tmp_path)
    d.services = discover_services()
    srv = next(iter(d.services.values()))
    calls = []
    monkeypatch.setattr(srv, "connect",
                        lambda **kw: calls.append(kw) or {"ok": True},
                        raising=False)
    rpc = RpcDb(tmp_path / "rpc.db")
    rpc.submit("connect", {"root": "C:\\docs"}, 30,
               service="local-knowledge")
    for rid2, tool, args, service in rpc.poll_requests(d.owner):
        d._dispatch(rid2, tool, args, service)
    row = rpc.db.execute("SELECT error FROM rpc_responses").fetchone()
    assert "not mounted" in row[0]
    assert not calls, "tagged miss ran on the wrong service"


def test_remount_hot_mounts_post_boot_install(tmp_path, monkeypatch):
    """#264: a service installed after daemon boot works on its first
    tagged call — _remount rescans entry points and hot-mounts instead
    of requiring a daemon restart."""
    import json as _json
    import types

    import confluence_knowledge.daemon as dm
    loc = types.ModuleType("loc")
    loc.probe_tool = lambda **kw: {"svc": "local-knowledge"}
    monkeypatch.setattr(dm, "discover_services",
                        lambda only=None: {"local-knowledge": loc})
    d = Daemon(tmp_path)
    d.services = {"confluence-knowledge": object()}   # boot predates it
    rpc = RpcDb(tmp_path / "rpc.db")
    rpc.submit("probe_tool", {}, 30, service="local-knowledge")
    for rid2, tool, args, service in rpc.poll_requests(d.owner):
        d._dispatch(rid2, tool, args, service)
    time.sleep(0.3)
    row = rpc.db.execute("SELECT result FROM rpc_responses").fetchone()
    assert _json.loads(row[0])["svc"] == "local-knowledge"
    assert "local-knowledge" in d.services


def test_untagged_legacy_request_single_mount(tmp_path, monkeypatch):
    """Rows submitted by pre-#206 proxies carry no service tag — with
    exactly one mount the fallback still routes them. Only tagged
    misses are hard errors."""
    import json as _json
    _fake_eps(monkeypatch, ["confluence-knowledge"])
    d = Daemon(tmp_path)
    d.services = discover_services()
    srv = next(iter(d.services.values()))
    monkeypatch.setattr(srv, "probe_tool", lambda **kw: {"svc": "conf"},
                        raising=False)
    rpc = RpcDb(tmp_path / "rpc.db")
    rpc.submit("probe_tool", {}, 30)                # service="" legacy
    for rid2, tool, args, service in rpc.poll_requests(d.owner):
        d._dispatch(rid2, tool, args, service)
    time.sleep(0.3)
    row = rpc.db.execute("SELECT result FROM rpc_responses").fetchone()
    assert _json.loads(row[0])["svc"] == "conf"


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
                        lambda r, env=None, **kw: spawned.append(True)
                        or _claim(rpc))
    def _claim(r):
        r.db.execute("INSERT OR REPLACE INTO daemon_lease "
                     "VALUES ('__daemon__','alive',12345,?)",
                     (time.time(),))
        r.db.commit()
    import confluence_knowledge.rpc as rpc_mod
    monkeypatch.setattr(rpc_mod, "ensure_daemon",
                        lambda r, env=None, **kw:
                        (_claim(r), spawned.append(True)))

    stop = tmp_path / ".watchdog.stop"
    def stop_soon():
        time.sleep(3.5)
        stop.write_text("1")
    threading.Thread(target=stop_soon, daemon=True).start()
    watchdog_run(tmp_path)
    assert spawned, "watchdog never attempted a respawn"


def test_ensure_daemon_respects_fresh_stop_file(tmp_path, monkeypatch):
    """Bug hunt: a `.watchdog.stop` written moments ago means a shutdown
    is mid-flight — ensure_daemon must NOT unlink it (that would let
    the watchdog resurrect the pair the user just killed). A STALE
    file is fair game: a later spawn is new intent."""
    import json as _json
    import socket as _sock

    import confluence_knowledge.rpc as rpc_mod
    from confluence_knowledge import settings
    monkeypatch.setattr(settings, "daemon_home", lambda: tmp_path)
    spawned = []
    monkeypatch.setattr(
        rpc_mod.RpcDb, "daemon_alive",
        lambda self: bool(spawned))        # dead → alive after spawn
    srv = _sock.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    (tmp_path / "http.json").write_text(
        _json.dumps({"port": srv.getsockname()[1], "token": "x"}))
    class _Proc:
        pid = 4242
    monkeypatch.setattr(rpc_mod.subprocess, "Popen",
                        lambda *a, **k: spawned.append(True) or _Proc())

    fresh = tmp_path / ".watchdog.stop"
    fresh.write_text("1")
    rpc_mod.ensure_daemon(RpcDb(tmp_path / "rpc.db"))
    assert fresh.exists(), "fresh stop-file was unlinked — mid-stop resurrect"
    assert spawned, "daemon itself should still spawn — tools stay up"

    import os
    os.utime(fresh, (0, 0))                     # ancient mtime → stale
    stamp = tmp_path / ".spawn.stamp"           # first spawn's stamp is
    if stamp.exists():                          # still fresh — age it so
        os.utime(stamp, (0, 0))                 # the second call spawns
    spawned.clear()                            # daemon "dead" again
    rpc_mod.ensure_daemon(RpcDb(tmp_path / "rpc.db"))
    assert not fresh.exists(), "stale stop-file should clear"
    assert spawned, "daemon never spawned after stale stop cleared"


def test_ensure_daemon_uses_bound_home_not_env(tmp_path, monkeypatch):
    """#273: the spawn workdir is where the RpcDb lives, not env
    daemon_home() — a watchdog spawned by an explicit-home daemon (its
    env lacks KNOWLEDGE_DAEMON_HOME) must respawn into the watched home,
    not the venv-default slot."""
    import confluence_knowledge.rpc as rpc_mod
    from confluence_knowledge import settings

    bound = tmp_path / "watched-home"
    elsewhere = tmp_path / "env-home"
    monkeypatch.setattr(settings, "daemon_home", lambda: elsewhere)
    spawned = []
    monkeypatch.setattr(rpc_mod, "_spawn_daemon",
                        lambda rpc, workdir, env=None:
                            spawned.append(workdir))
    monkeypatch.setattr(rpc_mod.RpcDb, "daemon_alive",
                        lambda self: bool(spawned))  # dead → alive post
    monkeypatch.setattr(rpc_mod, "_await_daemon", lambda *a, **k: None)
    rpc_mod.ensure_daemon(rpc_mod.RpcDb(bound / "rpc.db"))
    assert spawned == [bound], spawned


def test_detached_spawn_kwargs_windows(monkeypatch):
    """#220: on Windows the daemon/watchdog must fully detach from the
    spawning console — `start_new_session` only maps to
    CREATE_NEW_PROCESS_GROUP, leaving the child console-attached so it
    dies when the agent's terminal or the IDE session closes. The fix
    uses DETACHED_PROCESS + CREATE_NO_WINDOW (+ BREAKAWAY from Job
    objects) with no start_new_session."""
    import os

    import confluence_knowledge.rpc as rpc_mod
    monkeypatch.setattr(os, "name", "nt")
    kw = rpc_mod.detached_spawn_kwargs()
    assert "start_new_session" not in kw, \
        "POSIX-style session detach does not detach a Windows console"
    assert kw["creationflags"], "Windows spawn must carry detach flags"
    assert kw["close_fds"]


def test_daemon_http_status_flags_stale(tmp_path, monkeypatch):
    """#226: a daemon alive on the port but reporting an OLDER package
    version than the installed one must read as not-ok — 'alive' is not
    'current'; `pip install -U` doesn't restart the running process."""
    import json

    from confluence_knowledge import registration as reg
    (tmp_path / "http.json").write_text(
        json.dumps({"port": 9, "token": "t"}))
    monkeypatch.setattr(reg, "_http_meta", lambda w=None:
                        {"port": 9, "token": "t"})
    monkeypatch.setattr(reg.socket if hasattr(reg, "socket") else
                        __import__("socket"), "socket", None) if False else None
    import socket
    class _Probe:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def settimeout(self, t): pass
        def connect(self, addr): pass
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Probe())
    monkeypatch.setattr(reg, "_daemon_services_version",
                        lambda meta: "0.11.0")
    import importlib.metadata as im
    monkeypatch.setattr(im, "version", lambda p: "0.11.2")
    c = reg.daemon_http_status()
    assert not c["ok"], "alive-but-stale daemon must flag not-ok"
    assert "0.11.0" in c["detail"] and "0.11.2" in c["detail"]


def test_daemon_http_status_healthy(tmp_path, monkeypatch):
    """#226: same-version daemon → ok with the version surfaced."""
    from confluence_knowledge import registration as reg
    monkeypatch.setattr(reg, "_http_meta", lambda w=None:
                        {"port": 9, "token": "t"})
    import socket
    class _Probe:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def settimeout(self, t): pass
        def connect(self, addr): pass
    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Probe())
    monkeypatch.setattr(reg, "_daemon_services_version",
                        lambda meta: "0.11.2")
    import importlib.metadata as im
    monkeypatch.setattr(im, "version", lambda p: "0.11.2")
    c = reg.daemon_http_status()
    assert c["ok"] and "0.11.2" in c["detail"]


def test_restart_daemon_clears_stop_file(tmp_path, monkeypatch):
    """#226: restart intent must remove .watchdog.stop — a fresh marker
    would leave the new daemon watchdog-less (the watchdog loop exits
    while it exists)."""
    from confluence_knowledge import watchdog
    (tmp_path / ".watchdog.stop").write_text("1")
    calls = []
    monkeypatch.setattr(watchdog, "stop_daemon",
                        lambda home, timeout=8: calls.append("stop"))
    monkeypatch.setattr(watchdog, "spawn_watchdog",
                        lambda home: calls.append("spawn"))
    import confluence_knowledge.rpc as rpc_mod
    monkeypatch.setattr(rpc_mod, "ensure_daemon",
                        lambda rpc, env=None, **kw:
                        calls.append("ensure"))
    watchdog.restart_daemon(tmp_path)
    assert calls == ["stop", "ensure", "spawn"], calls
    assert not (tmp_path / ".watchdog.stop").exists()


def test_registered_transport(tmp_path, monkeypatch):
    """#226: doctor rewrites the entry with the user's own transport —
    url entries are http, command entries are stdio."""
    import json

    from confluence_knowledge import registration as reg
    mcp = tmp_path / "mcp.json"
    mcp.write_text(json.dumps(
        {"servers": {"confluence-knowledge": {"url": "http://x"}}}))
    monkeypatch.setattr(reg, "targets", lambda *a, **k: [mcp])
    assert reg.registered_transport() == "http"
    mcp.write_text(json.dumps(
        {"servers": {"confluence-knowledge": {"command": "py"}}}))
    assert reg.registered_transport() == "stdio"
    monkeypatch.setattr(reg, "targets", lambda *a, **k: [])
    assert reg.registered_transport() is None


def test_stdio_entry_prefers_pythonw_on_windows(tmp_path, monkeypatch):
    """#226: win32 proxies spawn per IDE window — python.exe would flash
    a console each time; pythonw.exe never does."""
    import sys

    from confluence_knowledge import registration as reg
    fake_py = tmp_path / "python.exe"
    fake_pyw = tmp_path / "pythonw.exe"
    fake_py.write_text("x"); fake_pyw.write_text("x")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(fake_py))
    e = reg._server_entry(None, "m", "E")
    assert e["command"].endswith("pythonw.exe"), e["command"]


def test_detached_spawn_kwargs_posix():
    """#220: POSIX keeps setsid — unchanged behaviour on macOS/Linux."""
    import os

    import confluence_knowledge.rpc as rpc_mod
    if os.name == "nt":
        return                     # covered by the windows case above
    assert rpc_mod.detached_spawn_kwargs() == {"start_new_session": True}


def test_popen_detached_breakaway_retry(monkeypatch):
    """#220: CREATE_BREAKAWAY_FROM_JOB can be denied when the parent's Job
    object disallows breakaway — the spawn must retry without it rather
    than fail the whole ensure_daemon."""
    import os

    import confluence_knowledge.rpc as rpc_mod
    monkeypatch.setattr(os, "name", "nt")
    class _Proc:
        pid = 4243
    flags_seen = []
    def _pop(argv, **kw):
        flags_seen.append(kw.get("creationflags", 0))
        if len(flags_seen) == 1:
            raise OSError("Access is denied")
        return _Proc()
    monkeypatch.setattr(rpc_mod.subprocess, "Popen", _pop)
    base = rpc_mod.detached_spawn_kwargs()["creationflags"]
    rpc_mod._popen_detached(["x"], creationflags=base)
    assert len(flags_seen) == 2, "the denied breakaway must retry once"
    assert flags_seen[0] & rpc_mod._BREAKAWAY_FLAG, \
        "first attempt must carry the breakaway flag"
    assert not flags_seen[1] & rpc_mod._BREAKAWAY_FLAG, \
        "the denied retry must drop breakaway, keep the detach bits"


def test_stop_never_signals_pid_reused_foreign_process(tmp_path):
    """Bug hunt: a stale .daemon.pid file (or a fresh lease row) that
    now points at an unrelated process must NOT be SIGTERMed — pid
    reuse hands the number to an innocent program."""
    import subprocess

    from confluence_knowledge.watchdog import stop_daemon
    victim = subprocess.Popen(["sleep", "60"])
    try:
        (tmp_path / ".daemon.pid").write_text(str(victim.pid))
        out = stop_daemon(tmp_path, timeout=1)
        assert out["daemon_signalled"] is False
        assert victim.poll() is None, "foreign process was killed"
    finally:
        victim.kill()


def test_http_services_endpoint_lists_mounts(tmp_path, monkeypatch):
    """/_services is the mount registry register() probes — it must name
    exactly the mounted set."""
    _fake_eps(monkeypatch, ["confluence-knowledge", "local-knowledge"])
    d = Daemon(tmp_path)
    d.services = discover_services()
    # mirror _serve_http's service list without binding a port
    assert sorted(d.services) == \
        ["confluence-knowledge", "local-knowledge"]
