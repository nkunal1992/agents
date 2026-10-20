"""Startup contract: `main()` must not load models eagerly (issue #159)."""

from sharepoint_knowledge import server_sp as server


def test_main_does_not_load_models(monkeypatch):
    calls = []
    for kind in ("embedder", "reranker", "sparse"):
        monkeypatch.setitem(server._pool._loaders, kind,
                            lambda h, c, k=kind: calls.append(k) or object())
    monkeypatch.setattr(server.mcp, "run", lambda: None)
    monkeypatch.setattr(server, "lower_priority", lambda: "priority: test")
    server.main()
    assert calls == []
    assert server._pool._reaper_started


def test_priority_lowered_on_start(monkeypatch):
    monkeypatch.setattr(server.mcp, "run", lambda: None)
    seen = []
    monkeypatch.setattr(server, "lower_priority", lambda: seen.append(1))
    server.main()
    assert seen == [1]
