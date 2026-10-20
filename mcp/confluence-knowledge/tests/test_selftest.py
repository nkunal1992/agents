"""selftest stage-runner tests — import/tools/health with fakes; the real
mini-RAG runs in e2e (it needs the downloaded models)."""
from __future__ import annotations

import sys
import types

from confluence_knowledge import selftest


def _fake_server(tools: set[str]) -> types.ModuleType:
    mod = types.ModuleType("fake_server")
    mod.__path__ = []
    mcp = types.SimpleNamespace()

    async def _list():
        return [types.SimpleNamespace(name=t) for t in tools]

    mcp.list_tools = _list
    mod.mcp = mcp
    return mod


def test_import_server_failure(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "nope_server_zzz",
                        None)  # import fails
    rep = selftest.run(tmp_path, "nope_server_zzz")
    assert rep["ok"] is False
    assert rep["stages"][0]["stage"] == "import_server"
    assert rep["stages"][0]["ok"] is False
    assert len(rep["stages"]) == 1  # bail early — nothing else can work


def test_tools_stage_missing_core(tmp_path, monkeypatch):
    mod = _fake_server({"search", "connect"})  # missing most core tools
    monkeypatch.setitem(sys.modules, "fake_server", mod)
    monkeypatch.setattr(selftest, "_mini_rag",
                        lambda home: {"stage": "mini_rag", "ok": True,
                                      "detail": "faked"})
    rep = selftest.run(tmp_path, "fake_server")
    tools = next(s for s in rep["stages"] if s["stage"] == "tools")
    assert tools["ok"] is False and "missing" in tools["detail"]
    assert rep["ok"] is False


def test_tools_stage_all_present(tmp_path, monkeypatch):
    mod = _fake_server(set(selftest.CORE_TOOLS))
    monkeypatch.setitem(sys.modules, "fake_server", mod)
    from confluence_knowledge import health
    monkeypatch.setattr(health, "health_checks",
                        lambda *a, **k: [{"flag": "deps", "ok": True,
                                          "detail": "x"}])
    monkeypatch.setattr(selftest, "_mini_rag",
                        lambda home: {"stage": "mini_rag", "ok": True,
                                      "detail": "faked"})
    rep = selftest.run(tmp_path, "fake_server")
    assert rep["ok"] is True
    names = [s["stage"] for s in rep["stages"]]
    assert names == ["import_server", "tools", "health", "mini_rag"]


def test_health_stage_flags_missing_models(tmp_path, monkeypatch):
    mod = _fake_server(set(selftest.CORE_TOOLS))
    monkeypatch.setitem(sys.modules, "fake_server", mod)
    rep = selftest.run(tmp_path, "fake_server")  # real health on empty home
    health = next(s for s in rep["stages"] if s["stage"] == "health")
    # an empty home can't have prepared models → health red (deps may vary)
    assert health["ok"] is False or rep["ok"] is False
    assert rep["ok"] is False


def test_report_shape_and_seconds(tmp_path, monkeypatch):
    mod = _fake_server(set(selftest.CORE_TOOLS))
    monkeypatch.setitem(sys.modules, "fake_server", mod)
    from confluence_knowledge import health
    monkeypatch.setattr(health, "health_checks",
                        lambda *a, **k: [{"flag": "deps", "ok": True,
                                          "detail": "x"}])
    monkeypatch.setattr(selftest, "_mini_rag",
                        lambda home: {"stage": "mini_rag", "ok": True,
                                      "detail": "faked"})
    rep = selftest.run(tmp_path, "fake_server")
    assert isinstance(rep["seconds"], float)
    assert all("seconds" in s for s in rep["stages"])
