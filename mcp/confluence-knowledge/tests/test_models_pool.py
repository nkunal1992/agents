"""ModelPool: lazy load, warm gate, idle unload, re-warm — all with
injected fake loaders and a controllable clock (no model files)."""

import threading

from confluence_knowledge.models import ModelPool


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _pool(tmp_path, clock=None, busy=lambda: False, on_unload=None,
          ready=None):
    p = ModelPool(lambda: tmp_path, "cli", busy_fn=busy,
                  on_unload=on_unload, ready_event=ready,
                  clock=clock or _Clock())
    calls = []
    for kind in ("embedder", "reranker", "sparse"):
        p._loaders[kind] = (
            lambda h, c, k=kind: calls.append(k) or object())
    return p, calls


def test_construction_loads_nothing(tmp_path):
    p, calls = _pool(tmp_path)
    assert calls == [] and not p.is_loaded() and not p.ready


def test_loaded_triggers_background_warm(tmp_path):
    p, calls = _pool(tmp_path)
    assert p.loaded(5.0) is True
    assert sorted(calls) == ["embedder", "reranker", "sparse"]
    assert p.is_loaded() and p.ready


def test_accessors_load_lazily(tmp_path):
    p, calls = _pool(tmp_path)
    obj = p.embedder()
    assert calls == ["embedder"] and p.embedder() is obj
    assert p.is_loaded()


def test_unload_if_idle(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_IDLE_UNLOAD_S", "60")
    clock = _Clock()
    fired = []
    p, _calls = _pool(tmp_path, clock=clock,
                      on_unload=lambda: fired.append(1),
                      ready=threading.Event())
    p.embedder()
    p._ready.set()
    clock.t = 30
    assert p.unload_if_idle() is False          # not idle long enough
    clock.t = 61
    assert p.unload_if_idle() is True
    assert not p.is_loaded() and not p.ready    # event cleared
    assert fired == [1]
    # reload after unload works
    assert p.loaded(5.0) is True and p.is_loaded()


def test_no_unload_while_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_IDLE_UNLOAD_S", "1")
    clock = _Clock()
    p, _ = _pool(tmp_path, clock=clock, busy=lambda: True)
    p.embedder()
    clock.t = 100
    assert p.unload_if_idle() is False and p.is_loaded()


def test_no_unload_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_IDLE_UNLOAD_S", "0")
    clock = _Clock()
    p, _ = _pool(tmp_path, clock=clock)
    p.embedder()
    clock.t = 10_000
    assert p.unload_if_idle() is False and p.is_loaded()


def test_rewarm_after_unload_via_warm_gate(tmp_path, monkeypatch):
    """Mirrors the server flow: warm gate cleared on unload → next
    loaded() re-warms instead of waiting forever."""
    monkeypatch.setenv("KNOWLEDGE_IDLE_UNLOAD_S", "10")
    clock = _Clock()
    ready = threading.Event()
    p, _ = _pool(tmp_path, clock=clock, ready=ready)
    assert p.loaded(5.0)
    clock.t = 100
    assert p.unload_if_idle()
    assert not ready.is_set()
    p._warm_started = False          # server re-arms via on_unload
    assert p.loaded(5.0) and p.ready
