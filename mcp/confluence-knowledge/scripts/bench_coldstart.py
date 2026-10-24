#!/usr/bin/env python3
"""Daemon cold-start bench (#255): spawn → serve → first answer, decomposed.

    python scripts/bench_coldstart.py [--corpus-files 12]

Measures, on a REAL daemon subprocess (local-knowledge service mounted):

  spawn→lease     — interpreter + module imports + rpc lease
  lease→http      — uvicorn mount + app build until TCP answers
  http→answer     — first `search` RPC: model warm + index open +
                    search-cache build + full retrieve — the number a
                    user feels on their first question
  answer→answer2  — the warm repeat (everything cached)

Seeds a tiny local-knowledge corpus + crawl first (daemon OFF for the
seed, then a real spawn) so the daemon answers over a genuine index.
Models come from KNOWLEDGE_MODELS_DIR (defaults to the shared store).

Teardown writes .watchdog.stop then SIGKILLs — the watchdog stands
down instead of respawning (stop_daemon protocol).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
sys.path.insert(0, str(PKG / "src"))
sys.path.insert(0, str(PKG.parent / "local-knowledge" / "src"))


def _gen_corpus(root: Path, n: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (root / f"doc{i}.md").write_text(
            f"# Document {i}\n\n"
            + " ".join(["deploy", "runbook", "verify", "owner",
                        "escalation", f"svc-{i}", "handover",
                        "incident", "review"] * (10 + i % 5)))


def _seed(home: Path, corpus: Path) -> dict:
    """connect + crawl with the daemon OFF — produces the scope
    registration + a real index the spawned daemon then serves."""
    os.environ["LOCAL_KNOWLEDGE_DAEMON"] = "0"
    os.environ["LOCAL_KNOWLEDGE_HOME"] = str(home)
    os.environ["LOCAL_KNOWLEDGE_WORKDIR"] = str(home / "wdir")
    from local_knowledge import server_local
    c = server_local.connect(str(corpus))
    scope = c["scope_handle"]
    j = server_local.start_crawl(scope)
    while True:
        r = server_local.crawl_step(scope, j["job_id"])
        if r["state"] != "running":
            break
        time.sleep(0.2)
    return {"scope_handle": scope, "job": j["job_id"], "state": r["state"]}


def _tcp_ok(port: int) -> bool:
    try:
        with socket.socket() as s:
            s.settimeout(0.3)
            s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus-files", type=int, default=12)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="cold-bench-"))
    shared = work / "shared"          # KNOWLEDGE_SHARED_HOME
    lk_home = work / "lkhome"         # LOCAL_KNOWLEDGE_HOME
    corpus = work / "corpus"
    os.environ["KNOWLEDGE_SHARED_HOME"] = str(shared)
    _gen_corpus(corpus, args.corpus_files)

    # seed index+scope in-process (daemon off)
    from confluence_knowledge.rpc import RpcDb
    from confluence_knowledge.settings import daemon_home
    info = _seed(lk_home, corpus)
    if info["state"] != "done":
        print(f"seed crawl ended {info['state']} — aborting",
              file=sys.stderr)
        return 1
    dhome = daemon_home()
    results = {"seed": info, "daemon_home": str(dhome), "runs": []}

    env = dict(os.environ)
    env["KNOWLEDGE_DAEMON_ROLE"] = "1"
    env["KNOWLEDGE_MODELS_DIR"] = os.environ.get(
        "KNOWLEDGE_MODELS_DIR", str(Path.home() / ".knowledge" / "models"))
    for run in range(args.runs):
        rpc = RpcDb(dhome / "rpc.db")
        t0 = time.monotonic()
        proc = subprocess.Popen(
            [sys.executable, "-m", "confluence_knowledge.daemon",
             str(dhome)],
            env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=open(dhome / "daemon.log", "ab"),  # noqa: SIM115
            start_new_session=True)
        # spawn → daemon lease live
        while not rpc.daemon_alive() and time.monotonic() - t0 < 60:
            time.sleep(0.05)
        t_lease = time.monotonic() - t0
        # lease → http answering (http.json port + TCP connect)
        t_http = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                meta = json.loads((dhome / "http.json").read_text())
                if _tcp_ok(meta["port"]):
                    t_http = time.monotonic() - t0
                    break
            except Exception:       # noqa: BLE001 — file pending
                pass
            time.sleep(0.05)
        # http → first search answer (cold models + cold cache)
        rid = rpc.submit("search",
                         {"scope_handle": info["scope_handle"],
                          "question": "deployment runbook owner"},
                         timeout_s=120, service="local-knowledge")
        t_a0 = time.monotonic()
        try:
            ans = rpc.wait(rid, timeout_s=120)
        except Exception as e:      # noqa: BLE001
            ans = {"_error": str(e)}
        t_first = time.monotonic() - t_a0
        # warm second answer
        rid2 = rpc.submit("search",
                          {"scope_handle": info["scope_handle"],
                           "question": "incident escalation review"},
                          timeout_s=60, service="local-knowledge")
        t_a1 = time.monotonic()
        try:
            rpc.wait(rid2, timeout_s=60)
            t_second = time.monotonic() - t_a1
        except Exception as e:      # noqa: BLE001
            t_second = -1
            ans["second_error"] = str(e)
        results["runs"].append({
            "spawn_to_lease_s": round(t_lease, 2),
            "spawn_to_http_s": round(t_http or -1, 2),
            "first_answer_s": round(t_first, 2),
            "warm_answer_s": round(t_second, 2),
            "evidence_rows": len(ans.get("evidence", []))
                if isinstance(ans, dict) else -1})
        # clean teardown: stop marker first → watchdog stands down
        try:
            from confluence_knowledge.watchdog import stop_daemon
            stop_daemon(dhome, timeout=5)
        except Exception:           # noqa: BLE001
            pass
        try:
            proc.kill()
        except OSError:
            pass
        # any respawned daemon/watchdog children from the run
        time.sleep(0.5)
        for f in (dhome / ".daemon.pid",):
            try:
                pid = int(f.read_text().strip())
                if pid != proc.pid:
                    os.kill(pid, signal.SIGKILL)
            except Exception:       # noqa: BLE001,S110
                pass

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
