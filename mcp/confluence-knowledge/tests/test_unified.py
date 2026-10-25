"""#265 — the unified `knowledge` MCP entry: live union of installed
services under <stem>__<tool>, prefix-routed calls, tools/list_changed
on install-set changes."""
from __future__ import annotations

import asyncio
import json
import types

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

import confluence_knowledge.daemon as dm
from confluence_knowledge.unified import UnifiedMCP, _stem


def _svc(mount: str, tools: dict[str, str] | None = None):
    """A fake installed service — a module-shaped object whose .mcp is a
    real FastMCP carrying the named probe tools."""
    m = FastMCP(mount)
    for tool_name in (tools or {"probe": mount}):
        def _register(n=tool_name):
            @m.tool(name=n)
            def t(q: str = "") -> dict:
                return {"svc": mount, "tool": n, "q": q}
        _register()
    mod = types.ModuleType(mount)
    mod.mcp = m
    return mod


def _install(monkeypatch, mounts: dict[str, object]):
    monkeypatch.setattr(dm, "discover_services",
                        lambda only=None: mounts)


def test_stem_mapping():
    assert _stem("confluence-knowledge") == "confluence"
    assert _stem("local-knowledge") == "local"
    assert _stem("other-thing") == "other_thing"


def test_list_tools_unions_with_stem_prefix(monkeypatch):
    _install(monkeypatch, {
        "confluence-knowledge": _svc("confluence-knowledge"),
        "local-knowledge": _svc("local-knowledge"),
    })
    u = UnifiedMCP(poll_s=0)
    names = {t.name for t in asyncio.run(u.list_tools())}
    assert names == {"confluence__probe", "local__probe"}


def test_call_tool_routes_by_prefix(monkeypatch):
    _install(monkeypatch, {
        "confluence-knowledge": _svc("confluence-knowledge"),
        "local-knowledge": _svc("local-knowledge"),
    })
    u = UnifiedMCP(poll_s=0)
    out = asyncio.run(u.call_tool("local__probe", {"q": "hi"}))
    payload = json.loads(out[0].text)
    assert payload["svc"] == "local-knowledge" and payload["q"] == "hi"


def test_call_tool_unknown_prefix_errors(monkeypatch):
    _install(monkeypatch, {"confluence-knowledge":
                           _svc("confluence-knowledge")})
    u = UnifiedMCP(poll_s=0)
    with pytest.raises(ToolError, match="no service for prefix"):
        asyncio.run(u.call_tool("local__probe", {}))


def test_call_tool_unprefixed_errors(monkeypatch):
    _install(monkeypatch, {"confluence-knowledge":
                           _svc("confluence-knowledge")})
    u = UnifiedMCP(poll_s=0)
    with pytest.raises(ToolError, match="<service>__<tool>"):
        asyncio.run(u.call_tool("probe", {}))


def test_list_tools_live_after_post_start_install(monkeypatch):
    """A service installed after the unified server started appears on
    the next tools/list — the zero-reload promise."""
    mounts = {"confluence-knowledge": _svc("confluence-knowledge")}
    _install(monkeypatch, mounts)
    u = UnifiedMCP(poll_s=0)
    assert {t.name for t in asyncio.run(u.list_tools())} == \
        {"confluence__probe"}
    mounts["local-knowledge"] = _svc("local-knowledge")
    assert {t.name for t in asyncio.run(u.list_tools())} == \
        {"confluence__probe", "local__probe"}


def test_tools_list_changed_fires_on_install_set_change(monkeypatch):
    """The watcher notifies only on a post-baseline change — the first
    scan must not spam it."""
    names = {"confluence-knowledge"}
    monkeypatch.setattr(UnifiedMCP, "_installed_names",
                        lambda self: set(names))
    u = UnifiedMCP(poll_s=0)
    fired = []
    monkeypatch.setattr(u, "_notify_tools_changed",
                        lambda: fired.append(True))
    seen = u._installed_names()                     # baseline
    seen = u._watch_once(seen)                      # no change — silent
    assert not fired
    names.add("local-knowledge")
    seen = u._watch_once(seen)                      # install → notify
    assert fired == [True]
    u._watch_once(seen)                             # steady — silent
    assert fired == [True]


def test_watcher_never_imports(monkeypatch):
    """Windows loader-lock deadlock guard: two threads importing
    native-extension modules concurrently can wedge (numpy's DllMain vs
    an anyio worker's DLL load — CI e2e hang). The watcher must detect
    install-set changes from entry-point NAMES only — imports belong to
    the request path."""
    def boom(only=None):
        raise AssertionError("watcher imported a service module")
    monkeypatch.setattr(dm, "discover_services", boom)
    monkeypatch.setattr(UnifiedMCP, "_installed_names",
                        lambda self: {"confluence-knowledge",
                                      "local-knowledge"})
    u = UnifiedMCP(poll_s=0)
    fired = []
    monkeypatch.setattr(u, "_notify_tools_changed",
                        lambda: fired.append(True))
    u._watch_once({"confluence-knowledge"})         # change → notify,
    assert fired == [True]                          # zero imports


def test_broken_service_does_not_hide_others(monkeypatch):
    """One service raising in list_tools must not blank the union."""
    good = _svc("confluence-knowledge")
    bad = types.ModuleType("local-knowledge")

    class Boom:
        async def list_tools(self):
            raise RuntimeError("import-time breakage")
    bad.mcp = Boom()
    _install(monkeypatch, {"confluence-knowledge": good,
                           "local-knowledge": bad})
    u = UnifiedMCP(poll_s=0)
    names = {t.name for t in asyncio.run(u.list_tools())}
    assert names == {"confluence__probe"}


def test_unified_registration_writes_module(tmp_path, monkeypatch):
    """`register` writes the unified entry BY DEFAULT (#269) —
    server_id=knowledge + the unified module, the same mount the daemon
    serves at /knowledge/mcp."""
    from confluence_knowledge import registration as reg
    mcp_json = tmp_path / "mcp.json"
    monkeypatch.setattr(reg, "targets", lambda *a, **k: [mcp_json])
    monkeypatch.setattr(reg, "ensure_gitignore", lambda *a, **k: None)
    reg.register("all")
    data = json.loads(mcp_json.read_text())
    entry = data["servers"]["knowledge"]
    assert entry["args"] == ["-m", "confluence_knowledge.unified"]
    assert "env" not in entry        # union proxy sets no service workdir


def test_per_service_registration_opt_out(tmp_path, monkeypatch):
    """`register --per-service` keeps the service's own id/module —
    both entries may coexist with the unified one."""
    from confluence_knowledge import registration as reg
    mcp_json = tmp_path / "mcp.json"
    monkeypatch.setattr(reg, "targets", lambda *a, **k: [mcp_json])
    monkeypatch.setattr(reg, "ensure_gitignore", lambda *a, **k: None)
    reg.register("all", server_id="confluence-knowledge",
                 module="confluence_knowledge.server")
    data = json.loads(mcp_json.read_text())
    entry = data["servers"]["confluence-knowledge"]
    assert entry["args"] == ["-m", "confluence_knowledge.server"]


def test_registration_flag_accepts_knowledge_only(tmp_path, monkeypatch):
    """Doctor must not nag a unified-only user: the registration check
    treats a `knowledge` entry as satisfying the service (#269)."""
    from confluence_knowledge import registration as reg
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps(
        {"servers": {"knowledge": {"command": "py", "args": ["-m", "u"]}}}))
    monkeypatch.setattr(reg, "targets", lambda *a, **k: [mcp_json])
    # registered_paths finds it via the knowledge id even though the
    # service's own id is absent
    assert reg.registered_paths("knowledge") == [mcp_json]
    assert reg.registered_paths("confluence-knowledge") == []
    from confluence_knowledge import health
    checks = health.health_checks(tmp_path, tmp_path,
                                  "confluence-knowledge",
                                  "confluence-knowledge")
    reg_flag = next(c for c in checks if c["flag"] == "registration")
    assert reg_flag["ok"], reg_flag
