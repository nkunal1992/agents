"""stress_local.py — the local-knowledge counterpart of stress_confluence.py.

No fake server needed: generates a seeded on-disk tree (mixed file types,
canary facts, an unreadable dir, symlink cases, an oversized file) and
drives the REAL local-knowledge MCP server exactly as the Copilot agent
does — connect → start_crawl → wait_for_crawl/search — while measuring:

    freeze latency   — any non-wait tool call > FREEZE_S during a crawl
    light p95        — status/crawl_step latency budget (<300 ms)
    laptop stall     — 50 ms probe overshoot > STALL_MS = machine froze
    resources        — server RSS / CPU via psutil sampler
    awareness        — progress blindness, wait_for_crawl change latency
    tombstoning      — mutated refresh deletes exactly the deleted set
    resilience       — cancel <5 s, kill -9 resume, memory pause/resume

    python scripts/stress_local.py --files 10000 --scenarios all \
        --profile worklaptop --models /tmp/ckperf --out /tmp/lk-stress

Scenarios: LIFE (fresh→unchanged→mutated refresh at --files), STORM,
CANCEL, KILL, MEMPAUSE, BATTERY, WINDOWS (the last five on a smaller
--files-fast tree — behaviour doesn't need scale). Everything is local;
safe to run on the work laptop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).parent
LOCAL_PKG = HERE.parent / "src"
ENGINE_PKG = HERE.parent.parent / "confluence-knowledge" / "src"

FREEZE_S = 2.0
LIGHT_P95_MS = 300.0
STALL_MS = 250.0
RSS_BUDGET_MB = 3072
CPU_BUDGET = 0.50
CRAWL_BUDGET_S = 21600.0        # 6 h ceiling per long crawl
CORE_BUDGET = {"native": os.cpu_count() or 8, "worklaptop": 8}
WORKLAPTOP_ENV = {"KNOWLEDGE_THREADS": "2"}
CANCEL_BUDGET_S = 5.0
CANARY_N = 10

_STALL_CODE = """
import json, sys, time
out = open(sys.argv[1], "a", buffering=1)
prev = time.monotonic()
while True:
    time.sleep(0.05)
    now = time.monotonic()
    out.write(json.dumps({"t": now, "overshoot_ms": (now - prev - 0.05) * 1000}) + "\\n")
    prev = now
"""

_BALLAST_CODE = """
import sys, time
from pathlib import Path
target_mb, stop = int(sys.argv[1]), Path(sys.argv[2])
chunks = []
try:
    for _ in range(target_mb // 64):
        chunks.append(bytearray(64 * 1024 * 1024))
        time.sleep(0.05)
    while not stop.exists():
        time.sleep(0.2)
finally:
    chunks.clear()
"""

# ---------------------------------------------------------------------------
# tree generation
# ---------------------------------------------------------------------------

_EXT_KINDS = [".md", ".txt", ".csv", ".py", ".html", ".json"]

_BODY = """# {name}

owner: team-{i}
service: svc-{i}

## Summary

This is seeded stress-corpus file {i}. The deployment codename for
service svc-{i} is {codeword}. Retention window {ret} days.

## Body

{lorem}
"""

_LOREM = ("local knowledge indexing stress text covering ingestion "
          "pipelines retrieval hybrid embeddings reranker grounding "
          "citations rollout runbook ownership sla latency budget ") * 8


def codeword(seed: int, i: int) -> str:
    return f"ORION-{seed}-{i:05d}"


def build_tree(root: Path, files: int, seed: int = 7) -> dict:
    """Deterministic tree: ~5-deep branching, mixed kinds, canaries,
    unreadable dir/file, symlinks, one oversized file, unicode names."""
    rng = random.Random(seed)
    root.mkdir(parents=True, exist_ok=True)
    canaries: list[Path] = []
    # coverage probes on text-extractable kinds only — prose written to a
    # .py/.json extension yields zero chunks by design, not by loss
    for ci in range(CANARY_N * 2):
        cp = root / f"canary-{ci:04d}.md"
        cp.write_text(_BODY.format(name=cp.stem, i=ci,
                                   codeword=codeword(seed, ci),
                                   ret=30, lorem=_LOREM))
        canaries.append(cp)
    n = 0
    dirs = [root]
    while n < files - CANARY_N * 2:
        d = rng.choice(dirs)
        # ~1 dir per 25 files, max depth 5 — gives real listing fan-out
        if rng.random() < 0.04 and len(d.relative_to(root).parts) < 5:
            sub = d / f"dept-{rng.randrange(40)}-{len(list(dirs))}"
            sub.mkdir(exist_ok=True)
            dirs.append(sub)
            continue
        ext = rng.choice(_EXT_KINDS)
        i = n
        p = d / f"doc-{i:05d}{ext}"
        body = _BODY.format(name=p.stem, i=i, codeword=codeword(seed, i),
                            ret=rng.randrange(7, 400), lorem=_LOREM)
        p.write_text(body)
        n += 1
    # edge cases: oversized file, unreadable dir+file, symlinks, unicode
    (root / "huge.bin").write_bytes(b"\x00" * (26 * 1024 * 1024))
    unreadable = root / "locked-dir"
    unreadable.mkdir(exist_ok=True)
    (unreadable / "hidden.md").write_text("secret doc")
    unreadable.chmod(0o000)
    (root / "unreadable-file.md").write_text("no read")
    (root / "unreadable-file.md").chmod(0o000)
    (root / "ユニコード-仕様書.md").write_text("unicode filename content")
    try:
        (root / "internal-link.md").symlink_to(root / "doc-00000.md")
        (root / "escape-link.md").symlink_to(Path("/etc/passwd"))
        (root / "broken-link.md").symlink_to(root / "no-such-file.md")
    except OSError:
        pass  # filesystems without symlink support — acceptable
    return {"canaries": canaries, "dirs": dirs}


def mutate_tree(root: Path, files: int, seed: int = 99) -> dict:
    """Delete 5 %, rename 5 %, modify 10 %, add 2 % new — returns the
    exact expected tombstone/keep sets for verdict checking."""
    rng = random.Random(seed)
    all_files = sorted(p for p in root.rglob("*") if p.is_file()
                       and not p.is_symlink())
    rng.shuffle(all_files)
    n = len(all_files)
    deleted = all_files[: n // 20]
    renamed = all_files[n // 20: n // 10]
    modified = all_files[n // 10: n // 10 + n // 10]
    for p in deleted:
        p.unlink()
    renames = []
    for p in renamed:
        np_ = p.with_name(f"{p.stem}-RENAMED{p.suffix}")
        p.rename(np_)
        renames.append(np_)
    for p in modified:
        p.write_text(p.read_text() + "\n\nmutation: content updated\n")
    added = []
    for i in range(max(1, n // 50)):
        np_ = root / f"added-{seed}-{i:04d}.md"
        np_.write_text(_BODY.format(name=np_.stem, i=900000 + i,
                                    codeword=codeword(seed, 900000 + i),
                                    ret=30, lorem=_LOREM))
        added.append(np_)

    def rel(ps):
        return {str(p.relative_to(root.parent)).replace("\\", "/")
                for p in ps}

    return {"deleted": rel(deleted), "renamed_old": rel(renamed),
            "renamed_new": rel([p.with_name(f"{p.stem}-RENAMED{p.suffix}")
                                for p in renamed]),
            "modified": rel(modified), "added": rel(added)}


# ---------------------------------------------------------------------------
# instruments (same measurement core as the confluence harness)


class Sampler:
    def __init__(self, pid: int):
        import psutil
        self.proc = psutil.Process(pid)
        self.samples: list[dict] = []
        self._stop = threading.Event()

    def _loop(self) -> None:
        import psutil
        while not self._stop.wait(0.5):
            try:
                mi = self.proc.memory_info()
                self.samples.append({
                    "t": time.time(),
                    "rss_mb": round(mi.rss / 1048576, 1),
                    "cpu_pct": round(self.proc.cpu_percent(), 1),
                    "threads": self.proc.num_threads(),
                    "sys_free_mb": round(
                        psutil.virtual_memory().available / 1048576),
                    "load1": round(os.getloadavg()[0], 1),
                })
            except Exception:  # noqa: BLE001 — process may have exited
                return

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> dict:
        self._stop.set()
        rss = [s["rss_mb"] for s in self.samples] or [0]
        cpu = [s["cpu_pct"] for s in self.samples] or [0]
        return {"peak_rss_mb": max(rss),
                "cpu_pct_p50": _pct(cpu, 50), "cpu_pct_p95": _pct(cpu, 95),
                "min_free_mb": min(s["sys_free_mb"]
                                   for s in self.samples)
                if self.samples else 0,
                "samples": self.samples}


class StallProbe:
    """Separate process that must wake every 50 ms — laptop-freeze proxy."""

    def __init__(self, out_file: Path):
        self.out_file = out_file
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _STALL_CODE, str(out_file)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> dict:
        self.proc.terminate()
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        overs = []
        if self.out_file.exists():
            for line in self.out_file.read_text().splitlines():
                try:
                    overs.append(json.loads(line)["overshoot_ms"])
                except json.JSONDecodeError:
                    pass
        return {"max_overshoot_ms": round(max(overs), 1) if overs else 0.0,
                "p95_overshoot_ms": round(_pct(overs, 95), 1) if overs else 0.0,
                "stall_events": sum(1 for o in overs if o > STALL_MS)}


def _pct(xs: list, q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q / 100))]


# ---------------------------------------------------------------------------
# context


def server_env(profile: str, home: Path, workdir: Path,
               extra: dict | None = None) -> dict:
    env = {**os.environ,
           "PYTHONPATH": f"{LOCAL_PKG.as_posix()}{os.pathsep}"
                         f"{ENGINE_PKG.as_posix()}",
           "LOCAL_KNOWLEDGE_HOME": str(home),
           "LOCAL_KNOWLEDGE_WORKDIR": str(workdir)}
    if profile == "worklaptop":
        env.update(WORKLAPTOP_ENV)
    env.update(extra or {})
    return env


def find_server_pid(cwd: Path, timeout: float = 20) -> int:
    import psutil
    want = cwd.resolve()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for p in psutil.process_iter(["pid", "cmdline", "cwd"]):
            try:
                if (p.cmdline()[-2:] == ["-m", "local_knowledge.server_local"]
                        and Path(p.cwd()).resolve() == want):
                    return p.pid
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        time.sleep(0.2)
    raise RuntimeError(f"server process not found under {cwd}")


class Ctx:
    def __init__(self, name: str, tree_root: Path, home: Path,
                 work: Path, out_dir: Path, profile: str):
        self.name = name
        self.tree_root = tree_root
        self.home, self.work = home, work
        self.out_dir, self.profile = out_dir, profile
        self.calls: list[dict] = []
        self.waits: list[dict] = []
        self.progress: list[dict] = []
        self.canary: list[dict] = []
        self.notes: list[str] = []
        self.sampler: Sampler | None = None
        self.stall: StallProbe | None = None
        self.session = None
        self.handle = None
        self.job = None
        self.crawl_active = False
        self.budget_s = CRAWL_BUDGET_S
        self.extra: dict = {}
        self.dead_transport_ok = False

    async def call(self, name: str, args: dict | None = None,
                   timeout: float = 30.0) -> dict:
        t = time.monotonic()
        try:
            r = await self.session.call_tool(
                name, args or {},
                read_timeout_seconds=timedelta(seconds=timeout))
        except Exception as e:
            if self.dead_transport_ok:
                return {"_error": f"transport: {e}"}
            raise
        dt = time.monotonic() - t
        entry = {"t": time.time(), "tool": name, "s": round(dt, 3),
                 "freeze": dt > FREEZE_S and name != "wait_for_crawl",
                 "during_crawl": self.crawl_active}
        self.calls.append(entry)
        text = r.content[0].text if r.content else ""
        if r.isError:
            entry["error"] = text[:200]
            return {"_error": text}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}

    async def prober(self, period: float = 1.0) -> None:
        while True:
            await self.call("status", timeout=10)
            if self.job:
                p = await self.call("crawl_step",
                                    {"scope_handle": self.handle,
                                     "job_id": self.job}, timeout=10)
                if "_error" not in p:
                    self.progress.append({"t": time.time(), "job": self.job,
                                          **{k: p.get(k) for k in (
                                              "state", "phase", "pages_done",
                                              "remaining", "rate_ppm",
                                              "eta_s", "pages_seen",
                                              "errors_n")}})
            await asyncio.sleep(period)

    async def agent_loop(self) -> None:
        while self.job:
            r = await self.call("wait_for_crawl",
                                {"scope_handle": self.handle,
                                 "job_id": self.job, "wait_s": 50},
                                timeout=60)
            if "_error" in r:
                return
            self.waits.append({"t": time.time(), "changed": r.get("changed"),
                               "state": r.get("state")})
            if r.get("state") not in ("running", "queued"):
                return

    async def run_crawl(self, refresh: bool = False, probes: bool = True,
                        timeout: float | None = None) -> dict:
        if not self.job:
            return {"wall_s": 0.0, "final": {"state": "no_job"}}
        self.crawl_active = True
        if timeout is None:
            timeout = self.budget_s
        tasks = [asyncio.create_task(self.agent_loop())]
        if probes:
            tasks.append(asyncio.create_task(self.prober()))
        t0 = time.monotonic()
        last = await self.call("crawl_step",
                               {"scope_handle": self.handle,
                                "job_id": self.job}, timeout=30)
        while (last.get("state") in ("running", "queued", None)
               and time.monotonic() - t0 < timeout):
            await asyncio.sleep(2)
            last = await self.call("crawl_step",
                                   {"scope_handle": self.handle,
                                    "job_id": self.job}, timeout=30)
        wall = time.monotonic() - t0
        self.crawl_active = False
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        first_page = next((p["t"] for p in self.progress
                           if (p.get("pages_done") or 0) > 0), None)
        start_t = next((c["t"] for c in self.calls
                        if c["tool"] == "start_crawl"), t0 + time.time())
        self.extra["progress_blind_s"] = round(
            (first_page - start_t) if first_page else wall, 1)
        first_changed = next((w["t"] for w in self.waits if w["changed"]),
                             None)
        self.extra["wait_first_change_s"] = (
            round(first_changed - start_t, 1) if first_changed else None)
        return {"wall_s": round(wall, 1), "final": last}

    async def connect_and_crawl(self, refresh: bool = False) -> None:
        c = await self.call("connect", {"root": str(self.tree_root)},
                            timeout=120)
        self.handle = c.get("scope_handle")
        self.extra["connect_resp"] = {k: c.get(k) for k in
                                      ("cached", "cached_pages",
                                       "stale_model", "auto_reindex")}
        j = await self.call("start_crawl",
                            {"scope_handle": self.handle,
                             "refresh": refresh}, timeout=30)
        self.job = j.get("job_id")

    async def canary_check(self, canaries: list[Path]) -> dict:
        """Coverage check — get_evidence must return each canary's chunks
        carrying its unique codeword (search ranks 8 near-identical
        synthetic chunks arbitrarily; the canary proves coverage, not
        ranking — that's the golden set's job)."""
        ok = 0
        for ci, p in enumerate(canaries[:CANARY_N]):
            word = codeword(self.extra["seed"], ci)
            rel = str(p.relative_to(self.tree_root.parent)).replace(
                "\\", "/")
            r = await self.call("get_evidence",
                                {"scope_handle": self.handle,
                                 "page_id": rel}, timeout=30)
            hit = word in json.dumps(r.get("chunks", []))
            self.canary.append({"file": p.name, "codeword": word,
                                "found": hit})
            ok += hit
        return {"canary_ok": ok, "canary_total": min(len(canaries),
                                                    CANARY_N)}

    def freeze_stats(self) -> dict:
        dc = [c for c in self.calls if c.get("during_crawl")]
        return {"freeze_events": sum(1 for c in dc if c["freeze"]),
                "calls": len(self.calls),
                "light_p95_s": round(_pct([c["s"] for c in self.calls
                                          if c["tool"] in
                                          ("status", "crawl_step")], 95), 3),
                "freeze_calls": [c for c in self.calls if c["freeze"]],
                "slowest": sorted(self.calls, key=lambda c: -c["s"])[:5]}


@asynccontextmanager
async def scenario_ctx(name: str, tree_root: Path, profile: str,
                       models: Path, out_dir: Path,
                       env_extra: dict | None = None):
    """MCP server + instruments bound to a generated tree."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    home = out_dir / "home"
    home.mkdir()
    (home / "models").symlink_to((models / "models").resolve()
                               if (models / "models").exists()
                               else models.resolve())
    work = out_dir / "work"
    work.mkdir()
    cwd = out_dir / "srv-cwd"
    cwd.mkdir()
    ctx = Ctx(name, tree_root, home, work, out_dir, profile)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = server_env(profile, home, work, env_extra)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "local_knowledge.server_local"], env=env, cwd=str(cwd))
    stall = StallProbe(out_dir / "stall.jsonl")
    try:
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            ctx.session = session
            ctx.stall = stall
            ctx.sampler = Sampler(find_server_pid(cwd))
            ctx.sampler.start()
            yield ctx
    finally:
        stall.proc.kill()


def finish(ctx: Ctx, extra_verdicts: dict | None = None) -> dict:
    res = {"scenario": ctx.name, **ctx.extra,
           "freezes": ctx.freeze_stats(),
           "calls": ctx.calls,
           "canary": ctx.canary,
           "canary_ok": sum(1 for c in ctx.canary if c["found"]),
           "canary_total": len(ctx.canary),
           "waits": ctx.waits[-40:],
           "progress_tail": ctx.progress[-40:],
           "notes": ctx.notes}
    if ctx.sampler:
        res["resources"] = ctx.sampler.stop()
    if ctx.stall:
        res["laptop"] = ctx.stall.stop()
    res["verdicts"] = {**baseline_verdicts(ctx, res), **(extra_verdicts or {})}
    (ctx.out_dir / "result.json").write_text(
        json.dumps(res, indent=2, default=str))
    return res


def v(cond: bool) -> str:
    return "PASS" if cond else "FAIL"


def baseline_verdicts(ctx: Ctx, res: dict) -> dict:
    fr = res.get("freezes", {})
    rec = res.get("resources", {}), res.get("laptop", {})
    cpu_budget_pct = CPU_BUDGET * 100 * CORE_BUDGET[ctx.profile]
    return {
        "tool calls <= 2s": v(fr.get("freeze_events", 0) == 0),
        "status/crawl_step p95 < 300ms": v(fr.get("light_p95_s", 9) < 0.3),
        "laptop stall <= 250ms": v(rec[1].get("stall_events", 9) == 0),
        "peak RSS < 3 GB": v(rec[0].get("peak_rss_mb", 9e9) < RSS_BUDGET_MB),
        "CPU <= ~50% budget": v(rec[0].get("cpu_pct_p50", 9e9)
                                <= cpu_budget_pct),
        "pages_done monotonic": v(_monotonic(ctx)),
    }


def _monotonic(ctx: Ctx) -> bool:
    from itertools import pairwise
    per_job: dict[str, list] = {}
    for p in ctx.progress:
        if p.get("pages_done") is not None:
            per_job.setdefault(p.get("job"), []).append(p["pages_done"])
    return all(b >= a for seq in per_job.values()
               for a, b in pairwise(seq))


def index_db(work: Path) -> Path:
    dbs = list((work / ".local_knowledge_index").glob("*.db"))
    assert dbs, "no index db"
    return dbs[0]


def active_page_ids(work: Path) -> set[str]:
    import sqlite3
    db = sqlite3.connect(index_db(work))
    try:
        return {r[0] for r in db.execute(
            "SELECT page_id FROM pages WHERE active=1")}
    finally:
        db.close()


def publish_counts(work: Path) -> dict:
    import sqlite3
    db = sqlite3.connect(index_db(work))
    try:
        rows = db.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'publish:%' "
            "ORDER BY key").fetchall()
        return json.loads(rows[-1][1]) if rows else {}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# scenarios


async def s_life(ctx: Ctx) -> dict:
    """LIFE: fresh crawl at --files → unchanged refresh → mutated refresh
    → tombstone exactness + canary + publish accounting."""
    tree = ctx.tree
    await ctx.connect_and_crawl()
    res = await ctx.run_crawl()
    ctx.extra["wall_s"] = res["wall_s"]
    ctx.extra["final"] = res["final"]
    done_pages = res["final"].get("pages_done", 0)

    # unchanged refresh — reuse path: wall ≈ fetch-only
    j = await ctx.call("start_crawl",
                       {"scope_handle": ctx.handle, "refresh": True},
                       timeout=30)
    ctx.job = j.get("job_id")
    r2 = await ctx.run_crawl()
    ctx.extra["refresh_wall_s"] = r2["wall_s"]

    # mutated refresh — the tombstoning proof
    await ctx.canary_check(tree["canaries"])
    mut = mutate_tree(ctx.tree_root, ctx.extra["files"],
                      seed=ctx.extra["seed"] + 1)
    ctx.notes.append(f"mutated: {len(mut['deleted'])} deleted, "
                     f"{len(mut['renamed_old'])} renamed, "
                     f"{len(mut['modified'])} modified, "
                     f"{len(mut['added'])} added")
    j = await ctx.call("start_crawl",
                       {"scope_handle": ctx.handle, "refresh": True},
                       timeout=30)
    ctx.job = j.get("job_id")
    r3 = await ctx.run_crawl()
    ctx.extra["mutated_wall_s"] = r3["wall_s"]

    active = active_page_ids(ctx.work)
    tomb_exact = mut["deleted"].isdisjoint(active) \
        and mut["renamed_old"].isdisjoint(active) \
        and mut["added"] <= active
    ctx.extra["tombstones"] = {
        "expected_deleted": len(mut["deleted"] | mut["renamed_old"]),
        "still_live": len((mut["deleted"] | mut["renamed_old"]) & active),
        "added_indexed": len(mut["added"] & active),
    }
    pub = publish_counts(ctx.work)
    ctx.notes.append(f"publish accounting: {pub}")
    # re-check canaries that survived the mutation — deleted ones must
    # be gone (tombstone), survivors still resolvable
    survivors = [p for p in tree["canaries"] if p.exists()]
    gone = len(tree["canaries"]) - len(survivors)
    ctx.notes.append(f"canaries: {gone} of {len(tree['canaries'])} "
                     f"mutated away")
    await ctx.canary_check(survivors)
    return {
        "fresh crawl done": v(res["final"].get("state") == "done"
                              and done_pages > 0),
        "refresh << fresh (reuse)": v(r2["wall_s"] < max(120,
                                        res["wall_s"] * 0.2)),
        "tombstones exact": v(tomb_exact),
        "publish accounting present": v(bool(pub)),
    }


async def s_storm(ctx: Ctx) -> dict:
    """STORM: search every 2 s while the fresh crawl runs — heavy/light
    contention (the refresh path finishes too fast to overlap)."""
    await ctx.connect_and_crawl()
    stop = False
    searches: list[float] = []

    async def storm() -> None:
        while not stop:
            await ctx.call("search",
                           {"scope_handle": ctx.handle,
                            "question": "deployment codename?"},
                           timeout=60)
            searches.append(ctx.calls[-1]["s"])
            await asyncio.sleep(2)

    storm_t = asyncio.create_task(storm())
    res = await ctx.run_crawl()
    stop = True
    storm_t.cancel()
    ctx.extra["wall_s"] = res["wall_s"]
    ctx.extra["storm_searches"] = len(searches)
    return {"searches landed mid-crawl": v(len(searches) >= 5),
            "searches responsive under crawl":
            v(all(s < FREEZE_S * 3 for s in searches))}


async def s_cancel(ctx: Ctx) -> dict:
    """CANCEL: cancel_crawl mid-crawl takes effect within seconds."""
    await ctx.connect_and_crawl()
    await asyncio.sleep(30)
    t = time.monotonic()
    r = await ctx.call("cancel_crawl",
                       {"scope_handle": ctx.handle, "job_id": ctx.job},
                       timeout=30)
    dt = time.monotonic() - t
    state = r.get("state")
    # crawl_step must not resurrect a cancelled job
    await asyncio.sleep(3)
    p = await ctx.call("crawl_step",
                       {"scope_handle": ctx.handle, "job_id": ctx.job},
                       timeout=10)
    ctx.extra["wall_s"] = 0
    return {"cancel applied <5s": v(dt < CANCEL_BUDGET_S
                                   and state == "cancelled"),
            "cancelled is terminal": v(p.get("state") == "cancelled")}


async def s_kill(ctx: Ctx) -> dict:
    """KILL: SIGKILL mid-crawl — new server resumes the durable job."""
    await ctx.connect_and_crawl()
    await asyncio.sleep(45)
    pid = ctx.sampler.proc.pid
    os.kill(pid, signal.SIGKILL)
    ctx.dead_transport_ok = True
    await asyncio.sleep(2)
    cwd2 = ctx.out_dir / "srv-cwd2"
    cwd2.mkdir(exist_ok=True)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = server_env(ctx.profile, ctx.home, ctx.work)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "local_knowledge.server_local"], env=env, cwd=str(cwd2))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s2:
        await s2.initialize()
        ctx.session = s2
        c = await ctx.call("connect", {"root": str(ctx.tree_root)},
                           timeout=60)
        ctx.handle = c.get("scope_handle")
        p = await ctx.call("crawl_step",
                           {"scope_handle": ctx.handle,
                            "job_id": ctx.job}, timeout=30)
        ctx.notes.append(f"post-kill crawl_step: {p.get('state')} "
                         f"worker={p.get('worker')}")
        res = await ctx.run_crawl()
    ctx.extra["wall_s"] = res["wall_s"]
    await ctx.canary_check(ctx.tree["canaries"])
    return {"resumed after kill -9":
            v(res["final"].get("state") == "done")}


async def s_mempause(ctx: Ctx) -> dict:
    """MEMPAUSE: ballast under the guard → paused → release → resume.

    Server env carries KNOWLEDGE_MIN_FREE_MB = current-free − ballast, so
    the pause must engage, and the pause ceiling (#172) guarantees it can
    never wedge."""
    await ctx.connect_and_crawl()
    await asyncio.sleep(10)
    stop = ctx.out_dir / "ballast-stop"
    ballast = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-c", _BALLAST_CODE, "1024", str(stop)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    paused_seen = False
    t0 = time.monotonic()
    while time.monotonic() - t0 < 90:
        p = await ctx.call("crawl_step",
                           {"scope_handle": ctx.handle, "job_id": ctx.job},
                           timeout=10)
        if "paused" in str(p.get("phase", "")):
            paused_seen = True
            break
        await asyncio.sleep(1)
    stop.touch()
    try:
        ballast.wait(15)
    except subprocess.TimeoutExpired:
        ballast.kill()
    res = await ctx.run_crawl()
    ctx.extra["wall_s"] = res["wall_s"]
    return {"memory guard paused the crawl": v(paused_seen),
            "resumed after pressure released":
            v(res["final"].get("state") == "done")}


async def s_battery(ctx: Ctx) -> dict:
    """BATTERY: KNOWLEDGE_FORCE_BATTERY=1 — saver halves fetch window and
    ~halves the embed duty cycle; crawl must still complete."""
    await ctx.connect_and_crawl()
    res = await ctx.run_crawl()
    ctx.extra["wall_s"] = res["wall_s"]
    phases = {p.get("phase", "") for p in ctx.progress}
    ctx.extra["final"] = res["final"]
    await ctx.canary_check(ctx.tree["canaries"])
    return {"battery saver engaged": v(
        any("battery" in p for p in phases)),
        "crawl done on battery": v(res["final"].get("state") == "done")}


async def s_windows(ctx: Ctx) -> dict:
    """WINDOWS: a second session starting the same job gets the lease
    answer — one worker only."""
    await ctx.connect_and_crawl()
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    cwd2 = ctx.out_dir / "srv-cwd2"
    cwd2.mkdir(exist_ok=True)
    env = server_env(ctx.profile, ctx.home, ctx.work)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "local_knowledge.server_local"], env=env, cwd=str(cwd2))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s2:
        await s2.initialize()
        c = await s2.call_tool("connect", {"root": str(ctx.tree_root)})
        handle = json.loads(c.content[0].text)["scope_handle"]
        j2 = await s2.call_tool("start_crawl",
                                {"scope_handle": handle})
        j2r = json.loads(j2.content[0].text)
    res = await ctx.run_crawl()
    ctx.extra["wall_s"] = res["wall_s"]
    worker = j2r.get("worker", "self")
    note = j2r.get("note", "")
    return {"second window deferred or joined": v(
        "another IDE window" in str(worker) + str(note)
        or j2r.get("job_id") != ctx.job),
        "crawl done": v(res["final"].get("state") == "done")}


SCENARIOS = {
    "LIFE": ("fresh + refresh + mutated refresh (tombstones)", s_life),
    "STORM": ("search storm during crawl", s_storm),
    "CANCEL": ("cancel mid-crawl", s_cancel),
    "KILL": ("kill -9 + resume", s_kill),
    "MEMPAUSE": ("memory ballast pause/resume", s_mempause),
    "BATTERY": ("forced battery saver", s_battery),
    "WINDOWS": ("two IDE windows, one job", s_windows),
}
FAST_SCENARIOS = ("STORM", "CANCEL", "KILL", "MEMPAUSE", "BATTERY",
                  "WINDOWS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=10000,
                    help="files in the LIFE tree")
    ap.add_argument("--files-fast", type=int, default=800,
                    help="files for behavioural scenarios")
    ap.add_argument("--scenarios", default="all",
                    help="all | comma list (LIFE,STORM,CANCEL,KILL,"
                         "MEMPAUSE,BATTERY,WINDOWS)")
    ap.add_argument("--profile", default="worklaptop",
                    choices=["worklaptop", "native"])
    ap.add_argument("--models", type=Path, required=True,
                    help="prepared models dir (…/models) or its parent")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    wanted = (list(SCENARIOS) if args.scenarios == "all"
              else [s.strip().upper() for s in args.scenarios.split(",")])
    rows = []

    for name in wanted:
        if name not in SCENARIOS:
            print(f"unknown scenario {name} — {sorted(SCENARIOS)}")
            return 2
        out_dir = args.out / name.lower()
        fast = name in FAST_SCENARIOS
        n = args.files_fast if fast else args.files
        # tree lives OUTSIDE the scenario dir — scenario_ctx wipes its
        # own dir, and chmod-000 fixtures resist rmtree
        tree_root = args.out / f"{name.lower()}-tree"
        if tree_root.exists():
            for pth in tree_root.rglob("*"):
                try:
                    pth.chmod(0o755)
                except OSError:
                    pass
            shutil.rmtree(tree_root)
        tree = build_tree(tree_root, n, seed=args.seed)
        env_extra = {}
        if name == "MEMPAUSE":
            import psutil
            free_mb = int(psutil.virtual_memory().available / 1048576)
            env_extra["KNOWLEDGE_MIN_FREE_MB"] = str(free_mb - 512)
        if name == "BATTERY":
            env_extra["KNOWLEDGE_FORCE_BATTERY"] = "1"

        print(f"\n=== {name} ({n} files) — {SCENARIOS[name][0]} ===",
              flush=True)
        try:
            res = asyncio.run(_run(name, tree_root, tree, args, out_dir,
                                   env_extra, n))
        except Exception as e:  # noqa: BLE001 — record, continue matrix
            res = {"scenario": name, "verdicts": {"scenario ran": "FAIL"},
                   "notes": [f"harness error: {type(e).__name__}: {e}"]}
            (out_dir / "result.json").write_text(
                json.dumps(res, indent=2, default=str))
        rows.append(res)
        print(json.dumps({k: v2 for k, v2 in res.items()
                          if k not in ("calls", "progress_tail", "waits",
                                       "canary", "resources")},
                         indent=2, default=str)[:2200], flush=True)

    lines = ["# local-knowledge stress report", "",
             f"profile={args.profile} files={args.files} "
             + f"files_fast={args.files_fast}", "",
             "| scenario | wall_s | RSS MB | freezes | canary | verdicts |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        vd = " ".join(f"{k}={v2}" for k, v2 in r.get("verdicts",
                                                   {}).items())
        lines.append(
            f"| {r['scenario']} | {r.get('wall_s', '?')} | "
            f"{r.get('resources', {}).get('peak_rss_mb', '?')} | "
            f"{r.get('freezes', {}).get('freeze_events', '?')} | "
            f"{r.get('canary_ok', '?')}/{r.get('canary_total', '?')} | "
            f"{vd} |")
    (args.out / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    fails = sum(1 for r in rows
                for x in r.get("verdicts", {}).values() if x == "FAIL")
    return 1 if fails else 0


async def _run(name, tree_root, tree, args, out_dir, env_extra, n):
    async with scenario_ctx(name, tree_root, args.profile, args.models,
                            out_dir, env_extra) as ctx:
        ctx.extra.update({"files": n, "seed": args.seed})
        ctx.tree = tree
        verdicts = await SCENARIOS[name][1](ctx)
        return finish(ctx, verdicts)


if __name__ == "__main__":
    sys.exit(main())
