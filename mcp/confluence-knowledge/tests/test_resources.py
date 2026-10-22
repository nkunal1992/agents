"""Resource policy: env parsing, lowered priority, batch planning."""

import os
import random

from confluence_knowledge import resources
from confluence_knowledge.resources import Limits, limits, plan_batches


def _calm(monkeypatch):
    """Neutral machine: mains power, idle CPU, ample RAM."""
    monkeypatch.setattr(resources, "_battery_active", lambda: False)
    monkeypatch.setattr(resources, "_cpu_pct", lambda: 30.0)
    monkeypatch.setattr(resources, "_avail_mb", lambda: 32768.0)


def test_default_threads_scale_with_cores(monkeypatch):
    _calm(monkeypatch)
    # half the cores, floored at 2, capped at 10
    for cpus, want in ((2, 2), (8, 4), (12, 6), (16, 8), (64, 10)):
        monkeypatch.setattr(os, "cpu_count", lambda c=cpus: c)
        monkeypatch.delenv("KNOWLEDGE_THREADS", raising=False)
        assert limits().threads == want
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert limits().threads == 2


def test_default_threads_halve_under_pressure(monkeypatch):
    _calm(monkeypatch)
    monkeypatch.setattr(os, "cpu_count", lambda: 20)
    assert limits().threads == 10
    monkeypatch.setattr(resources, "_battery_active", lambda: True)
    assert limits().threads == 5
    monkeypatch.setattr(resources, "_battery_active", lambda: False)
    monkeypatch.setattr(resources, "_cpu_pct", lambda: 85.0)
    assert limits().threads == 5


def test_default_max_batch_tracks_free_ram(monkeypatch):
    _calm(monkeypatch)
    monkeypatch.delenv("KNOWLEDGE_MAX_BATCH", raising=False)
    for mb, want in ((1024.0, 4), (4096.0, 8), (12000.0, 12),
                     (32768.0, 16)):
        monkeypatch.setattr(resources, "_avail_mb", lambda a=mb: a)
        assert limits().max_batch == want
    monkeypatch.setattr(resources, "_cpu_pct", lambda: 90.0)
    assert limits().max_batch == 8
    monkeypatch.setenv("KNOWLEDGE_MAX_BATCH", "6")   # env wins over auto
    assert limits().max_batch == 6


def test_limits_env_overrides_and_invalid(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_THREADS", "7")
    monkeypatch.setenv("KNOWLEDGE_BATCH_TOKENS", "3000000")
    monkeypatch.setenv("KNOWLEDGE_MAX_BATCH", "8")
    monkeypatch.setenv("KNOWLEDGE_IDLE_UNLOAD_S", "60")
    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "3")
    monkeypatch.setenv("KNOWLEDGE_MIN_FREE_MB", "2048")
    monkeypatch.setenv("KNOWLEDGE_BATTERY_SAVER", "0")
    # freeze pressure sampling — env parsing is under test, not live load
    monkeypatch.setattr(resources, "_pressured", lambda: False)
    assert limits() == Limits(threads=7, batch_tokens=3_000_000,
                              max_batch=8, idle_unload_s=60.0,
                              fetch_concurrency=3, min_free_mb=2048,
                              battery_saver=False)
    monkeypatch.setenv("KNOWLEDGE_THREADS", "bogus")
    monkeypatch.setenv("KNOWLEDGE_MAX_BATCH", "0")   # clamps to ≥1
    monkeypatch.setenv("KNOWLEDGE_FETCH_CONCURRENCY", "99")  # clamps to ≤16
    lim = limits()
    assert lim.threads == _default() and lim.max_batch == 1 \
        and lim.fetch_concurrency == 16


def _default():
    return max(2, min(10, (os.cpu_count() or 4) // 2))


def test_lower_priority_calls_nice(monkeypatch):
    if os.name == "nt":
        return  # POSIX path only
    monkeypatch.delenv("KNOWLEDGE_NO_NICE", raising=False)
    seen = []
    monkeypatch.setattr(os, "nice", lambda d: seen.append(d))
    assert resources.lower_priority() == "priority: nice +10"
    assert seen == [10]


def test_lower_priority_tolerates_oserror(monkeypatch):
    if os.name == "nt":
        return
    monkeypatch.delenv("KNOWLEDGE_NO_NICE", raising=False)

    def boom(d):
        raise OSError("not permitted")
    monkeypatch.setattr(os, "nice", boom)
    assert "unchanged" in resources.lower_priority()


def test_lower_priority_opt_out(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_NO_NICE", "1")
    called = []
    if os.name != "nt":
        monkeypatch.setattr(os, "nice", lambda d: called.append(d))
    assert "KNOWLEDGE_NO_NICE" in resources.lower_priority()
    assert not called


def test_plan_batches_properties(monkeypatch):
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    rng = random.Random(0)
    for _ in range(200):
        n = rng.randint(0, 40)
        lengths = [rng.randint(1, 3000) for _ in range(n)]
        budget = rng.choice([512, 2048, 8192])
        mb = rng.choice([4, 16])
        batches = plan_batches(lengths, budget, mb)
        flat = sorted(i for b in batches for i in b)
        assert flat == list(range(n))          # permutation, no drops
        for b in batches:
            if len(b) > 1:
                m = max(lengths[i] for i in b)
                assert len(b) * m * m <= budget   # B×L² bound
                assert len(b) <= mb


def test_plan_batches_oversized_singleton():
    # batches hold indices in descending-length order
    assert plan_batches([100_000, 10, 20], 1024, 16) == [[0], [2, 1]]
    assert plan_batches([], 1024, 16) == []
    assert plan_batches([10], 5, 16) == [[0]]
