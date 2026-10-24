"""Shared model lifecycle for all three knowledge servers.

One ModelPool per server process owns the embedder / reranker / sparse
sessions. Models load lazily on first use (never at process start — an
idle IDE window costs ~0.2 GB, not ~2 GB), and a reaper thread releases
them after `KNOWLEDGE_IDLE_UNLOAD_S` seconds without activity, as long
as no crawl worker is running. The next call re-warms transparently;
tools keep their existing "loading" note contract while that happens.
"""

from __future__ import annotations

import gc
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .resources import limits
from .retrieval import LocalEmbedder, LocalReranker, LocalSparseEmbedder
from .settings import SetupError


class ModelPool:
    def __init__(self, home_fn: Callable[[], Path], cli: str,
                 busy_fn: Callable[[], bool] | None = None,
                 on_unload: Callable[[], None] | None = None,
                 ready_event: threading.Event | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._home_fn = home_fn
        self._cli = cli
        self._busy_fn = busy_fn or (lambda: False)
        self._on_unload = on_unload
        self._clock = clock
        self._ready = ready_event if ready_event is not None \
            else threading.Event()
        self._lock = threading.Lock()
        self._warm_lock = threading.Lock()
        self._warm_started = False
        self._reaper_started = False
        self._last_use = 0.0
        self._embedder: LocalEmbedder | None = None
        self._reranker: LocalReranker | None = None
        self._sparse: LocalSparseEmbedder | None = None
        # injectable for tests — {kind: factory(home, cli)}
        self._loaders: dict[str, Callable] = {
            "embedder": LocalEmbedder,
            "reranker": LocalReranker,
            "sparse": LocalSparseEmbedder,
        }
        # per-kind creation locks — different models may load in parallel
        # while the same kind still single-loads (#255)
        self._load_locks = {k: threading.Lock() for k in self._loaders}

    def _touch(self) -> None:
        self._last_use = self._clock()

    def _get(self, kind: str):
        attr = f"_{kind}"
        obj = getattr(self, attr)
        if obj is not None:
            self._touch()
            return obj
        # the loader runs under a per-kind lock, not the pool lock —
        # session init used to serialize all three models (~0.6s) even
        # though they build independent ONNX graphs (#255)
        with self._load_locks.setdefault(kind, threading.Lock()):
            obj = getattr(self, attr)
            if obj is None:
                obj = self._loaders[kind](self._home_fn(), self._cli)
                setattr(self, attr, obj)
        self._touch()
        return obj

    def embedder(self) -> LocalEmbedder:
        return self._get("embedder")

    def reranker(self) -> LocalReranker:
        return self._get("reranker")

    def sparse(self) -> LocalSparseEmbedder:
        return self._get("sparse")

    @property
    def ready(self) -> bool:
        """True once a warm-up pass has completed (successfully or not —
        a missing model is reported by the tool that needs it)."""
        return self._ready.is_set()

    def is_loaded(self) -> bool:
        return self._embedder is not None

    def _warm(self) -> None:
        try:
            if limits().battery_saver and limits().on_battery():
                # battery saver: sequential keeps the session-init spike
                # small on a throttled CPU
                for k in ("embedder", "reranker", "sparse"):
                    self._get(k)
            else:
                with ThreadPoolExecutor(
                        max_workers=3,
                        thread_name_prefix="model-load") as pool:
                    futs = [pool.submit(self._get, k)
                            for k in ("embedder", "reranker", "sparse")]
                    for f in futs:
                        f.result()
        except SetupError:
            pass  # reported by the tool that needs the model
        finally:
            self._ready.set()

    def start_warmup(self) -> None:
        """Load the ONNX sessions off the request path; the first load can
        take longer than an IDE's request timeout."""
        with self._warm_lock:
            if self._warm_started:
                return
            self._warm_started = True
        threading.Thread(target=self._warm, name="model-warmup",
                         daemon=True).start()

    def loaded(self, wait: float | None) -> bool:
        self.start_warmup()
        return self._ready.wait(wait)

    def unload_if_idle(self) -> bool:
        """Release every model once idle past the configured TTL and no
        crawl is running. In-flight callers keep their references alive
        until they return — only the pool's ownership is dropped."""
        idle_s = limits().idle_unload_s
        if idle_s <= 0 or self._busy_fn():
            return False
        with self._lock:
            if (self._embedder is None and self._reranker is None
                    and self._sparse is None):
                return False
            if self._clock() - self._last_use < idle_s:
                return False
            self._embedder = self._reranker = self._sparse = None
            self._ready.clear()
            with self._warm_lock:
                self._warm_started = False
        gc.collect()
        if self._on_unload is not None:
            self._on_unload()
        return True

    def start_reaper(self, interval_s: float = 60.0) -> None:
        """Daemon thread that calls unload_if_idle periodically."""
        with self._warm_lock:
            if self._reaper_started:
                return
            self._reaper_started = True

        def loop() -> None:
            while True:
                time.sleep(interval_s)
                try:
                    self.unload_if_idle()
                except Exception:  # noqa: BLE001,S110 — reaper must never die
                    pass

        threading.Thread(target=loop, name="model-reaper",
                         daemon=True).start()


# ----------------------------------------------------------------------
# Shared pool — one model set per process (#206 consolidation). All three
# services pin byte-identical ONNX models; three daemons used to hold
# ~6 GB of duplicate sessions when all warm. Under the multi-service
# daemon the mounted modules share a single pool; standalone daemons get
# the same object via the same call. Per-service callbacks compose:
# every busy_fn must report quiet before the reaper unloads, and every
# on_unload runs so each module can drop its cached handles.

_SHARED_READY = threading.Event()
_SHARED_POOL: ModelPool | None = None
_SHARED_BUSY: list[Callable[[], bool]] = []
_SHARED_UNLOAD: list[Callable[[], None]] = []


def shared_ready() -> threading.Event:
    """The one 'models warm' event every mounted module should expose —
    a service that shares the pool shares its readiness."""
    return _SHARED_READY


def shared_pool(home_fn: Callable[[], Path], cli: str,
                busy_fn: Callable[[], bool] | None = None,
                on_unload: Callable[[], None] | None = None,
                clock: Callable[[], float] = time.monotonic) -> ModelPool:
    global _SHARED_POOL
    if busy_fn is not None and busy_fn not in _SHARED_BUSY:
        _SHARED_BUSY.append(busy_fn)
    if on_unload is not None and on_unload not in _SHARED_UNLOAD:
        _SHARED_UNLOAD.append(on_unload)
    if _SHARED_POOL is None:
        _SHARED_POOL = ModelPool(
            home_fn, cli,
            busy_fn=lambda: any(f() for f in _SHARED_BUSY),
            on_unload=lambda: [f() for f in _SHARED_UNLOAD],
            ready_event=_SHARED_READY, clock=clock)
    return _SHARED_POOL
