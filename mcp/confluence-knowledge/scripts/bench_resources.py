"""Resource-usage benchmarks for the knowledge engine (issue #159).

Each scenario runs in a FRESH subprocess so peak RSS is clean. Requires
prepared models — point CK_MODEL_HOME at a prepared home, e.g.

    CK_MODEL_HOME=/tmp/ckperf python scripts/bench_resources.py

Usage:
    python scripts/bench_resources.py            # all scenarios
    python scripts/bench_resources.py page_40_huge idle_cpu equivalence

The launcher prints a JSON summary; every scenario also prints its own
JSON line. Thresholds are documented per scenario in SCENARIOS.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("CK_MODEL_HOME", "/tmp/ckperf"))
PKG = Path(__file__).resolve().parents[1] / "src"

# scenario name → (worker code, pass criteria for the emitted JSON)
_WORKER = r"""
import json, os, resource, sys, time
from pathlib import Path
sys.path.insert(0, {pkg!r})
os.environ.setdefault("HF_HUB_OFFLINE", "1")
HOME = Path({home!r})
scenario = {scenario!r}


def rss_mb():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


def cpu_s():
    u = resource.getrusage(resource.RUSAGE_SELF)
    return u.ru_utime + u.ru_stime


from confluence_knowledge.retrieval import (
    LocalEmbedder, LocalReranker, load_sparse_or_none)

out = {{"scenario": scenario, "cpus": os.cpu_count()}}
base = rss_mb()
t = time.time()
emb = LocalEmbedder(HOME)
out["load_embed_s"] = round(time.time() - t, 1)
out["rss_after_embed_load_mb"] = round(rss_mb())

normal = ("The deployment pipeline promotes builds through staging and "
          "production after the integration suite passes. ") * 30
huge = ("| col a | col b | col c | value |\n") * 400

if scenario == "warm_all":
    LocalReranker(HOME)
    load_sparse_or_none(HOME)   # sparse measured only where it runs #320
    out["rss_all_models_mb"] = round(rss_mb())
elif scenario.startswith("page"):
    n = int(scenario.split("_")[1])
    texts = [normal] * n
    if scenario.endswith("huge"):
        texts[0] = huge
    t = time.time()
    emb.embed(texts)
    out["embed_s"] = round(time.time() - t, 1)
    out["peak_rss_mb"] = round(rss_mb())
elif scenario == "idle_cpu":
    emb.embed([normal] * 8)
    c0 = cpu_s()
    time.sleep(3)
    out["idle_cpu_s_per_3s"] = round(cpu_s() - c0, 3)
elif scenario == "rerank25_huge":
    rr = LocalReranker(HOME)
    docs = [normal] * 24 + [huge]
    t = time.time()
    rr.score("how does promotion work?", docs)
    out["rerank_s"] = round(time.time() - t, 1)
    out["peak_rss_mb"] = round(rss_mb())
elif scenario == "equivalence":
    # micro-batched vs one-at-a-time. ONNX pooling isn't perfectly
    # mask-immune, so padded batches drift ~1-2% — same as the old
    # batched path. Expect row cosine ≥ 0.98, not bitwise equality.
    import numpy as np
    texts = [normal * i for i in (1, 2, 5)] + [huge, "short"]
    batched = emb.embed(texts)
    singles = np.vstack([emb.embed([t]) for t in texts])
    cos = (batched * singles).sum(1)
    out["min_row_cosine"] = round(float(cos.min()), 6)
    out["row_cosines"] = [round(float(c), 4) for c in cos]
elif scenario == "throughput_200":
    texts = [normal + str(i) for i in range(200)]
    t = time.time()
    emb.embed(texts)
    out["embed_200_s"] = round(time.time() - t, 1)
    out["peak_rss_mb"] = round(rss_mb())
elif scenario == "idle_unload":
    import gc, subprocess
    from confluence_knowledge.models import ModelPool
    from confluence_knowledge.retrieval import LocalEmbedder as LE, \
        LocalReranker as LR
    # ru_maxrss is a high-water mark — use live RSS via ps
    def live_rss():
        return float(subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True, text=True).stdout.strip()) / 1024
    os.environ["KNOWLEDGE_IDLE_UNLOAD_S"] = "5"
    pool = ModelPool(lambda: HOME, "confluence-knowledge")
    pool.embedder(); pool.reranker(); pool.sparse()
    loaded = live_rss()
    time.sleep(8)
    assert pool.unload_if_idle()
    gc.collect()
    after = live_rss()
    out["rss_loaded_mb"] = round(loaded)
    out["rss_after_unload_mb"] = round(after)
    out["freed_mb"] = round(loaded - after)
print(json.dumps(out))
"""

SCENARIOS = {
    "warm_all": "baseline model RSS (informational)",
    "page_40_huge": "worst-case page — peak RSS should stay ≤ ~2.6 GB",
    "rerank25_huge": "worst-case rerank — peak RSS should stay ≤ ~2.6 GB",
    "idle_cpu": "no busy-spinning — idle CPU ≈ 0",
    "equivalence": "micro-batches ≈ one-at-a-time (row cosine ≥ 0.98)",
    "throughput_200": "200 normal chunks wall-clock (informational)",
    "idle_unload": "models released after idle TTL",
    "server_idle": "server process with no calls — RSS ≤ ~250 MB, ~0 CPU",
}


def _run_subprocess(code: str, env_extra: dict | None = None,
                    timeout: int = 300) -> dict:
    env = dict(os.environ, PYTHONPATH=PKG.as_posix(), **(env_extra or {}))
    p = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, env=env, timeout=timeout, check=False)
    for line in reversed(p.stdout.splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"worker produced no JSON: {p.stdout[-2000:]} "
                       f"{p.stderr[-2000:]}")


def _model_scenario(name: str) -> dict:
    code = _WORKER.format(pkg=str(PKG), home=str(HOME), scenario=name)
    return _run_subprocess(code)


def _server_idle() -> dict:
    """Launch the real server, hold stdin open, measure RSS/CPU while it
    waits for tool calls — it must not load models."""
    code = (f"import sys; sys.path.insert(0, {str(PKG)!r}); "
            "from confluence_knowledge.server import main; main()")
    env = dict(os.environ, PYTHONPATH=PKG.as_posix(),
               CONF_KNOWLEDGE_HOME=str(HOME))
    p = subprocess.Popen([sys.executable, "-c", code],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env)
    try:
        time.sleep(20)
        out = subprocess.run(
            ["ps", "-o", "rss=,%cpu=", "-p", str(p.pid)],
            capture_output=True, text=True, check=False).stdout.split()
        rss_mb = float(out[0]) / 1024 if out else -1
        cpu_pct = float(out[1]) if len(out) > 1 else -1
        return {"scenario": "server_idle", "rss_mb": round(rss_mb),
                "cpu_pct": cpu_pct, "pid": p.pid}
    finally:
        p.kill()


def main() -> int:
    wanted = sys.argv[1:] or list(SCENARIOS)
    results = []
    for name in wanted:
        t0 = time.time()
        try:
            r = (_server_idle() if name == "server_idle"
                 else _model_scenario(name))
        except Exception as e:  # noqa: BLE001 — report and continue
            r = {"scenario": name, "error": f"{type(e).__name__}: {e}"}
        r["wall_s"] = round(time.time() - t0, 1)
        r["expect"] = SCENARIOS.get(name, "?")
        results.append(r)
        print(json.dumps(r), flush=True)
    print(json.dumps({"results": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
