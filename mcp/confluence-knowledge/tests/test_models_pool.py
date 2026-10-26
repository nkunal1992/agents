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


def test_sparse_unavailable_degrades_to_none(tmp_path):
    """#287: a platform wheel gap (e.g. py_rust_stemmers on win_arm64)
    degrades the sparse leg to None — crawl/search keep dense+rerank."""
    from confluence_knowledge.retrieval import SparseUnavailable
    p, _calls = _pool(tmp_path)
    attempts = []
    def missing(h, c):
        attempts.append(1)
        raise SparseUnavailable("sparse leg unavailable: py_rust_stemmers")
    p._loaders["sparse"] = missing
    assert p.sparse() is None
    assert "py_rust_stemmers" in p.sparse_missing
    assert p.sparse() is None            # short-circuits, no retry storm
    assert len(attempts) == 1
    assert p.loaded(5.0) is True         # warm completes despite the gap


def test_sparse_unavailable_resets_on_unload(tmp_path, monkeypatch):
    """#287: idle unload clears the degrade flag — a later reinstall
    can heal instead of being masked by the cached failure."""
    from confluence_knowledge.retrieval import SparseUnavailable
    monkeypatch.setenv("KNOWLEDGE_IDLE_UNLOAD_S", "5")
    clock = _Clock()
    p, _ = _pool(tmp_path, clock=clock, ready=threading.Event())
    p._loaders["sparse"] = lambda h, c: (
        (_ for _ in ()).throw(SparseUnavailable("gone")))
    assert p.sparse() is None and p.sparse_missing
    clock.t = 10
    p._embedder = object()          # something must be loaded to unload
    assert p.unload_if_idle() is True
    assert p.sparse_missing is None
    p._loaders["sparse"] = lambda h, c: object()
    assert p.sparse() is not None   # healed: retry loads the new leg


def test_sparse_unavailable_does_not_swallow_other_kinds(tmp_path):
    """#287: SparseUnavailable from a non-sparse loader still propagates —
    the degrade is scoped to the leg it was designed for."""
    import pytest

    from confluence_knowledge.retrieval import SparseUnavailable
    p, _ = _pool(tmp_path)
    p._loaders["embedder"] = lambda h, c: (
        (_ for _ in ()).throw(SparseUnavailable("weird")))
    with pytest.raises(SparseUnavailable):
        p.embedder()
