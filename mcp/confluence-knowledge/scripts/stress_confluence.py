"""Confluence stress harness (#169): the real MCP server vs a fake work DC.

Drives `python -m confluence_knowledge.server` over stdio exactly like the
Copilot agent (connect → start_crawl → wait_for_crawl / crawl_step → search)
while `_fake_dc_work.py` serves a realistic, optionally hostile Confluence
Data Center tree in its own subprocess.

Instruments that run alongside every crawl:

- responsiveness prober — `status`/`crawl_step` every second, `search`
  periodically; a call over FREEZE_S is a freeze event
- agent loop — `wait_for_crawl(wait_s=50)` like the agent file prescribes,
  so we measure how quickly a user-visible update can surface
- laptop-stall probe — a separate process that should wake every 50 ms;
  overshoot over STALL_MS means the machine itself was starved
- resource sampler — psutil on the server process tree + system memory
- progress auditor — pages_done monotonicity and ETA accuracy
- canary check — search facts planted by the tree generator, verifying the
  crawl really indexed (not just completed fast)

Usage:
    python scripts/stress_confluence.py --pages 200 --profile worklaptop \
        --scenarios all --models /tmp/ckperf --out /tmp/ck-stress
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent / "src"
sys.path.insert(0, str(HERE))

import _fake_dc_work as fake


def _embed_dir_name() -> str:
    """Active embed model's on-disk dir (#249 — follows
    KNOWLEDGE_EMBED_MODEL / the spec registry)."""
    try:
        from confluence_knowledge.retrieval import active_embed_spec
        return active_embed_spec()["repo"].split("/")[-1]
    except Exception:
        return "snowflake-arctic-embed-xs"


FREEZE_S = 2.0          # tool call slower than this = freeze event
LIGHT_P95_MS = 300.0    # status/crawl_step p95 budget
STALL_MS = 250.0        # laptop-probe overshoot budget
RSS_BUDGET_MB = 3072    # server RSS ceiling
CPU_BUDGET = 0.50       # sustained fraction of the core budget
CRAWL_BUDGET_S = 3600.0   # per-scenario crawl watch limit (scaled by pages)
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
# fake-DC + server lifecycle


def start_fake(pages: int, hostile: bool, out_dir: Path,
               latency: float = 0.05) -> tuple[subprocess.Popen, str, Path]:
    """Spawn the fake work DC as a subprocess; returns (proc, base, log)."""
    prof, logf = out_dir / "dc-profile.json", out_dir / "dc-requests.jsonl"
    cmd = [sys.executable, str(HERE / "_fake_dc_work.py"),
           "--pages", str(pages), "--port", "0",
           "--profile-file", str(prof), "--log-file", str(logf),
           "--clean-latency", str(latency)]
    if hostile:
        cmd.append("--hostile")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    for _ in range(200):
        if prof.exists():
            break
        if proc.poll() is not None:
            raise RuntimeError("fake DC died on startup")
        time.sleep(0.05)
    base = json.loads(prof.read_text(encoding="utf-8"))["base_url"]
    return proc, base, logf


def mutate_dc(base: str, pct: float = 10.0, seed: int = 99) -> dict:
    req = urllib.request.Request(
        f"{base}/_control/mutate?pct={pct}&seed={seed}", method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


def server_env(profile: str, home: Path, workdir: Path,
               extra: dict | None = None) -> dict:
    env = {**os.environ, "PYTHONPATH": PKG.as_posix(),
           "CONF_KNOWLEDGE_HOME": str(home),
           "CONF_KNOWLEDGE_WORKDIR": str(workdir)}
    if profile == "worklaptop":
        env.update(WORKLAPTOP_ENV)
    env.update(extra or {})
    return env


def find_server_pid(cwd: Path, timeout: float = 20) -> int:
    """The mcp stdio client doesn't expose the spawned PID — find it by the
    unique cwd we gave StdioServerParameters."""
    import psutil
    want = cwd.resolve()  # macOS /tmp → /private/tmp
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                if (p.cmdline()[-2:] == ["-m", "confluence_knowledge.server"]
                        and Path(p.cwd()).resolve() == want):
                    return p.pid
            except (psutil.Error, IndexError, OSError):
                continue
        time.sleep(0.1)
    raise RuntimeError("server process not found")


# ---------------------------------------------------------------------------
# instruments


class Sampler:
    """psutil thread: server RSS/CPU/threads + system free mem, 500 ms."""

    def __init__(self, pid: int):
        import psutil
        self.proc = psutil.Process(pid)
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self.proc.cpu_percent()  # prime

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
                                   for s in self.samples) if self.samples else 0,
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
            for line in self.out_file.read_text(encoding="utf-8").splitlines():
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
# MCP context


class Ctx:
    """One scenario's live context: MCP session + instruments + results."""

    def __init__(self, name: str, base: str, logf: Path, out_dir: Path,
                 model_home: Path, profile: str):
        self.name, self.base, self.logf = name, base, logf
        self.out_dir, self.model_home, self.profile = out_dir, model_home, profile
        self.calls: list[dict] = []          # tool-name latency log
        self.waits: list[dict] = []          # agent-loop wait_for_crawl log
        self.progress: list[dict] = []       # crawl_step snapshots
        self.canary: list[dict] = []
        self.notes: list[str] = []
        self.sampler: Sampler | None = None
        self.stall: StallProbe | None = None
        self.session = None
        self.handle = None
        self.job = None
        self.crawl_active = False   # set while a job runs — freeze window
        self.budget_s = CRAWL_BUDGET_S
        self.extra: dict = {}

    async def call(self, name: str, args: dict | None = None,
                   timeout: float = 30.0) -> dict:
        t = time.monotonic()
        r = await self.session.call_tool(
            name, args or {},
            read_timeout_seconds=timedelta(seconds=timeout))
        dt = time.monotonic() - t
        entry = {"t": time.time(), "tool": name, "s": round(dt, 3),
                 # wait_for_crawl is a deliberate long-poll — never a freeze
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

    # instruments ------------------------------------------------------

    async def prober(self, period: float = 1.0) -> None:
        """Light-tool responsiveness prober — the freeze detector."""
        while True:
            await self.call("status", timeout=10)
            if self.job:
                p = await self.call("crawl_step",
                                    {"scope_handle": self.handle,
                                     "job_id": self.job}, timeout=10)
                if "_error" not in p:
                    self.progress.append({"t": time.time(),
                                          "job": self.job, **{
                        k: p.get(k) for k in (
                            "state", "phase", "pages_done", "remaining",
                            "rate_ppm", "eta_s", "pages_seen")}})
            await asyncio.sleep(period)

    async def agent_loop(self) -> None:
        """What the agent file prescribes: wait_for_crawl until terminal."""
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

    async def run_crawl(self, refresh: bool = False,
                        probes: bool = True,
                        timeout: float | None = None) -> dict:
        """Wait for the started job to reach a terminal state, with the
        agent-loop and prober instruments running alongside."""
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
        # awareness metrics: how long the user saw no progress at all, and
        # how long the agent's first wait took to report a change
        first_page = next((p["t"] for p in self.progress
                           if (p.get("pages_done") or 0) > 0), None)
        start_t = next((c["t"] for c in self.calls
                        if c["tool"] == "start_crawl"), t0 + time.time())
        self.extra["progress_blind_s"] = round(
            (first_page - start_t) if first_page else wall, 1)
        first_changed = next((w["t"] for w in self.waits if w["changed"]),
                             None)
        self.extra["wait_first_change_s"] = round(
            (first_changed - start_t), 1) if first_changed else None
        return {"wall_s": round(wall, 1), "final": last}

    async def connect_and_crawl(self, refresh: bool = False) -> None:
        c = await self.call("connect", {"root":
                                        f"{self.base}/pages/"
                                        f"viewpage.action?pageId={fake.ROOT_ID}"},
                            timeout=120)
        self.handle = c.get("scope_handle")
        self.extra["connect_s"] = next(
            (c2["s"] for c2 in reversed(self.calls) if c2["tool"] == "connect"),
            0)
        j = await self.call("start_crawl",
                            {"scope_handle": self.handle, "refresh": refresh},
                            timeout=30)
        self.job = j.get("job_id")
        self.extra["start_resp"] = j

    async def canary_check(self, n: int = CANARY_N) -> dict:
        """Pick n indexed pages and ask who owns their service."""
        import random as _r
        rng = _r.Random(1)
        t = fake.build_tree(self.extra["pages"], self.extra["seed"])
        try:
            raw = await asyncio.to_thread(
                urllib.request.urlopen,
                self.base + "/_control/forbidden", timeout=5)
            forb = set(json.loads(raw.read())["forbidden"])
        except Exception:  # noqa: BLE001 — control endpoint optional
            forb = set()
        reachable = [i for i in t.pages if i != t.root
                     and not forb.intersection(
                         t.pages[i].get("ancestors", []) + [i])]
        self.extra["forbidden_pages"] = sorted(forb)
        ids = rng.sample(reachable, min(n, len(reachable)))
        ok = 0
        for pid in ids:
            r = await self.call("search",
                                {"scope_handle": self.handle,
                                 "question": f"who owns service-{pid}?"},
                                timeout=60)
            hit = any(e.get("page_id") == pid or f"service-{pid}" in
                      json.dumps(e)
                      for e in r.get("evidence", []))
            self.canary.append({"pid": pid, "found": hit})
            if hit:
                ok += 1
        return {"canary_ok": ok, "canary_total": len(ids)}

    def freeze_stats(self) -> dict:
        dc = [c for c in self.calls if c.get("during_crawl")]
        return {"freeze_events": sum(1 for c in dc if c["freeze"]),
                "calls": len(self.calls),
                "light_p95_s": round(_pct([c["s"] for c in self.calls
                                          if c["tool"] in
                                          ("status", "crawl_step")], 95), 3),
                "freeze_calls": [c for c in self.calls if c["freeze"]],
                "slowest": sorted(self.calls, key=lambda c: -c["s"])[:5]}

    def request_stats(self) -> dict:
        if not self.logf.exists():
            return {}
        rows = [json.loads(x) for x in self.logf.read_text(encoding="utf-8").splitlines()
                if x.strip()]
        by_status: dict[int, int] = {}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        in_flight = [r["in_flight"] for r in rows]
        retry_after_s = sum(1 for r in rows if r["status"] == 429)
        return {"requests": len(rows), "by_status": by_status,
                "max_in_flight": max(in_flight) if in_flight else 0,
                "retry_after_hits": retry_after_s}


# ---------------------------------------------------------------------------
# scenario runner


@asynccontextmanager
async def scenario_ctx(name: str, pages: int, seed: int, hostile: bool,
                       profile: str, models: Path, out_dir: Path,
                       env_extra: dict | None = None):
    """Spin up fake DC + MCP server + instruments; yield a Ctx."""
    if out_dir.exists():  # fresh scenario dir — a rerun never merges state
        import shutil
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    proc, base, logf = start_fake(pages, hostile, out_dir)
    work = out_dir / "work"
    work.mkdir()
    cwd = out_dir / "srv-cwd"
    cwd.mkdir()
    ctx = Ctx(name, base, logf, out_dir, models, profile)
    ctx.extra.update({"pages": pages, "seed": seed, "hostile": hostile,
                      "profile": profile})
    ctx.budget_s = max(CRAWL_BUDGET_S, pages * 10)  # scale with tree size
    (models / "profile.json").write_text(json.dumps(
        {"env": "datacenter", "base_url": base, "email": None,
         "credential_ref": "stress"}), encoding="utf-8")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = server_env(profile, models, work,
                     {"CONF_KNOWLEDGE_TOKEN": fake.TOKEN,
                      **(env_extra or {})})
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "confluence_knowledge.server"], env=env, cwd=str(cwd))
    stall = StallProbe(out_dir / "stall.jsonl")
    try:
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            await session.initialize()
            ctx.session = session
            ctx.stall = stall
            pid = find_server_pid(cwd)
            ctx.sampler = Sampler(pid)
            ctx.sampler.start()
            yield ctx
    finally:
        stall.proc.kill()
        proc.kill()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.terminate()


def finish(ctx: Ctx, extra_verdicts: dict | None = None) -> dict:
    res = {"scenario": ctx.name, **ctx.extra,
           "freezes": ctx.freeze_stats(),
           "calls": ctx.calls,
           "requests": ctx.request_stats(),
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
    (ctx.out_dir / "result.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
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
        "peak RSS < 3 GB": v(rec[0].get("peak_rss_mb", 9e9)
                             < RSS_BUDGET_MB),
        "CPU <= ~50% budget": v(rec[0].get("cpu_pct_p50", 9e9)
                                <= cpu_budget_pct),
        "canary all found": v(res.get("canary_total", 0) > 0
                              and res.get("canary_ok") == res["canary_total"]),
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


# --- scenarios -------------------------------------------------------------


async def s_clean(ctx: Ctx) -> dict:
    """S1/S2: first crawl on the clean fake DC (native / worklaptop)."""
    await ctx.connect_and_crawl()
    res = await ctx.run_crawl()
    ctx.extra["wall_s"] = res["wall_s"]
    ctx.extra["final"] = res["final"]
    await ctx.canary_check()
    return {}


async def s_hostile(ctx: Ctx) -> dict:
    """S3: first crawl through latency, 429s, 5xx, hangs, 403s."""
    await ctx.connect_and_crawl()
    res = await ctx.run_crawl(timeout=3600)
    ctx.extra["wall_s"] = res["wall_s"]
    ctx.extra["final"] = res["final"]
    await ctx.canary_check()
    rq = ctx.request_stats()["by_status"]
    return {"survived 429/5xx/hang mix":
            v(rq.get(429, 0) + rq.get(500, 0) + rq.get(502, 0)
              + rq.get(503, 0) + rq.get(504, 0) > 0)}


async def s_refresh_unchanged(ctx: Ctx) -> dict:
    """S4: crawl → refresh with nothing changed (near-zero rework)."""
    await ctx.connect_and_crawl()
    first = await ctx.run_crawl()
    ctx.calls.clear()
    ctx.logf.write_text("", encoding="utf-8")  # reset request log for the refresh leg
    j = await ctx.call("start_crawl",
                       {"scope_handle": ctx.handle, "refresh": True},
                       timeout=30)
    ctx.job = j.get("job_id")
    second = await ctx.run_crawl()
    ctx.extra.update({"first_wall_s": first["wall_s"],
                      "refresh_wall_s": second["wall_s"]})
    await ctx.canary_check()
    return {"refresh <= 1.5x pure fetch time":
            v(second["wall_s"] <= max(30.0, first["wall_s"] * 0.5))}


async def s_refresh_mutated(ctx: Ctx) -> dict:
    """S5: crawl → mutate 10% + deletes/moves → refresh → spot-check."""
    await ctx.connect_and_crawl()
    await ctx.run_crawl()
    summ = mutate_dc(ctx.base, pct=10.0)
    ctx.notes.append(f"mutated: {summ}")
    # searchability of the old snapshot must hold during the refresh
    j = await ctx.call("start_crawl",
                       {"scope_handle": ctx.handle, "refresh": True},
                       timeout=30)
    ctx.job = j.get("job_id")
    probe = await ctx.call("search",
                           {"scope_handle": ctx.handle,
                            "question": "who owns service-1000003?"},
                           timeout=60)
    ctx.notes.append("mid-refresh search: "
                     + ("ok" if "_error" not in probe else "ERROR"))
    second = await ctx.run_crawl()
    ctx.extra["refresh_wall_s"] = second["wall_s"]
    await ctx.canary_check()
    return {"snapshot searchable mid-refresh": v("_error" not in probe)}


async def s_search_storm(ctx: Ctx) -> dict:
    """S6: crawl while an agent search lands every 2 s (heavy/light mix)."""
    await ctx.connect_and_crawl()

    async def storm() -> None:
        for _ in range(40):
            await ctx.call("search",
                           {"scope_handle": ctx.handle,
                            "question": "what is the release approval policy?"},
                           timeout=60)
            await asyncio.sleep(2)

    storm_t = asyncio.create_task(storm())
    res = await ctx.run_crawl()
    storm_t.cancel()
    await asyncio.gather(storm_t, return_exceptions=True)
    ctx.extra["wall_s"] = res["wall_s"]
    await ctx.canary_check()
    searches = [c["s"] for c in ctx.calls if c["tool"] == "search"]
    ctx.extra["search_p95_s"] = round(_pct(searches, 95), 2)
    return {"search p95 during crawl < 10s": v(_pct(searches, 95) < 10.0)}


async def s_two_windows(ctx: Ctx) -> dict:
    """S7: a second server process (second 'IDE window') starts the same
    crawl — the lease must make exactly one of them the worker."""
    await ctx.connect_and_crawl()
    await asyncio.sleep(2)  # let window 1 own the job
    cwd2 = ctx.out_dir / "srv-cwd2"
    cwd2.mkdir(exist_ok=True)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = server_env(ctx.profile, ctx.model_home, ctx.out_dir / "work",
                     {"CONF_KNOWLEDGE_TOKEN": fake.TOKEN})
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "confluence_knowledge.server"],
                                   env=env, cwd=str(cwd2))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s2:
        await s2.initialize()
        t = time.monotonic()
        c = await s2.call_tool(
            "connect", {"root": f"{ctx.base}/pages/"
                                f"viewpage.action?pageId={fake.ROOT_ID}"},
            read_timeout_seconds=timedelta(seconds=60))
        h2 = json.loads(c.content[0].text).get("scope_handle")
        j2 = await s2.call_tool(
            "start_crawl", {"scope_handle": h2},
            read_timeout_seconds=timedelta(seconds=30))
        j2r = json.loads(j2.content[0].text)
        ctx.notes.append(f"window2 start_crawl: {j2r}")
        ctx.extra["window2_worker"] = j2r.get("worker", "self")
        ctx.extra["window2_start_s"] = round(time.monotonic() - t, 1)
        res = await ctx.run_crawl()
    ctx.extra["wall_s"] = res["wall_s"]
    # intended semantic (agent files): a second window's start_crawl makes
    # a NEW job that abandons the first — exactly one active worker. The
    # loser's crawl_step must then show 'abandoned', not 'running'.
    abandoned_ok = (j2r.get("job_id") != ctx.job
                    and res["final"].get("state") in
                    ("abandoned", "done", "error", "cancelled"))
    return {"single active worker (new job supersedes)": v(abandoned_ok)}


async def s_cancel(ctx: Ctx) -> dict:
    """S8: cancel mid-crawl — stop latency, snapshot intact, restart."""
    await ctx.connect_and_crawl()
    await asyncio.sleep(6)  # crawl is underway
    t = time.monotonic()
    r = await ctx.call("cancel_crawl",
                       {"scope_handle": ctx.handle, "job_id": ctx.job},
                       timeout=30)
    ctx.extra["cancel_call_s"] = round(time.monotonic() - t, 2)
    deadline = time.monotonic() + 30
    state = r.get("state")
    while state != "cancelled" and time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        p = await ctx.call("crawl_step",
                           {"scope_handle": ctx.handle, "job_id": ctx.job},
                           timeout=10)
        state = p.get("state")
    stop_s = time.monotonic() - t
    probe = await ctx.call("search",
                           {"scope_handle": ctx.handle,
                            "question": "release approval policy?"},
                           timeout=60)
    ctx.extra["cancel_stop_s"] = round(stop_s, 2)
    # restart — must reuse what was already done
    j = await ctx.call("start_crawl", {"scope_handle": ctx.handle}, timeout=30)
    ctx.job = j.get("job_id")
    res = await ctx.run_crawl()
    ctx.extra["wall_s"] = res["wall_s"]
    await ctx.canary_check()
    return {"cancel stop < 5s": v(stop_s < CANCEL_BUDGET_S),
            "snapshot searchable after cancel": v("_error" not in probe)}


async def s_kill_resume(ctx: Ctx) -> dict:
    """S9: kill -9 the server mid-crawl, relaunch, crawl_step resumes."""
    await ctx.connect_and_crawl()
    await asyncio.sleep(8)
    pid = ctx.sampler.proc.pid
    os.kill(pid, signal.SIGKILL)
    ctx.dead_transport_ok = True  # first session's pipe is dead by design
    await asyncio.sleep(2)
    # new server process, SAME workdir — session needs replacing
    cwd2 = ctx.out_dir / "srv-cwd3"
    cwd2.mkdir(exist_ok=True)
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = server_env(ctx.profile, ctx.model_home, ctx.out_dir / "work",
                     {"CONF_KNOWLEDGE_TOKEN": fake.TOKEN})
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "confluence_knowledge.server"],
                                   env=env, cwd=str(cwd2))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s2:
        await s2.initialize()
        ctx.session = s2
        c = await ctx.call("connect",
                           {"root": f"{ctx.base}/pages/"
                                    f"viewpage.action?pageId={fake.ROOT_ID}"},
                           timeout=60)
        ctx.handle = c.get("scope_handle")
        p = await ctx.call("crawl_step",
                           {"scope_handle": ctx.handle,
                            "job_id": ctx.job}, timeout=30)
        ctx.notes.append(f"post-kill crawl_step: {p.get('state')} "
                         f"worker={p.get('worker')}")
        res = await ctx.run_crawl()
    ctx.extra["wall_s"] = res["wall_s"]
    await ctx.canary_check()
    return {"resumed after kill -9":
            v(res["final"].get("state") == "done")}


async def s_mem_pressure(ctx: Ctx) -> dict:
    """S10: ballast pushes free RAM under the guard mid-crawl, then frees."""
    await ctx.connect_and_crawl()
    await asyncio.sleep(5)
    # trip the guard with ~1 GB ballast by pointing the guard threshold
    # just under current free memory (server was started with a high
    # KNOWLEDGE_MIN_FREE_MB for this scenario — see env_extra)
    stop = ctx.out_dir / "ballast-stop"
    ballast = await asyncio.to_thread(
        subprocess.Popen,
        [sys.executable, "-c", _BALLAST_CODE, "1024", str(stop)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    paused_seen = False
    t0 = time.monotonic()
    while time.monotonic() - t0 < 60:
        p = await ctx.call("crawl_step",
                           {"scope_handle": ctx.handle, "job_id": ctx.job},
                           timeout=10)
        if "paused" in str(p.get("phase", "")):
            paused_seen = True
            break
        await asyncio.sleep(1)
    stop.touch()
    ballast.wait(15)
    res = await ctx.run_crawl()
    ctx.extra["wall_s"] = res["wall_s"]
    await ctx.canary_check()
    return {"memory guard paused the crawl": v(paused_seen),
            "resumed after pressure released":
            v(res["final"].get("state") == "done")}


async def s_idle_unload(ctx: Ctx) -> dict:
    """S11: after the crawl, models must unload and idle CPU must be ~0."""
    await ctx.connect_and_crawl()
    await ctx.run_crawl()
    rss_loaded = ctx.sampler.proc.memory_info().rss / 1048576
    await asyncio.sleep(75)  # KNOWLEDGE_IDLE_UNLOAD_S=60 via env_extra
    rss_idle = ctx.sampler.proc.memory_info().rss / 1048576
    ctx.extra.update({"rss_loaded_mb": round(rss_loaded),
                      "rss_idle_mb": round(rss_idle),
                      "freed_mb": round(rss_loaded - rss_idle)})
    return {"models unloaded when idle": v(rss_idle < rss_loaded * 0.6)}


async def s_battery(ctx: Ctx) -> dict:
    """S12: battery-saver forced on — crawl still finishes, slower+gentler."""
    await ctx.connect_and_crawl()
    res = await ctx.run_crawl()
    ctx.extra["wall_s"] = res["wall_s"]
    return {}  # the CPU/RSS budgets in baseline_verdicts are the check


SCENARIOS = {
    "S1": ("clean crawl, native profile",
           {"fn": s_clean, "hostile": False, "profile": "native"}),
    "S2": ("clean crawl, worklaptop profile",
           {"fn": s_clean, "hostile": False, "profile": "worklaptop"}),
    "S3": ("hostile crawl, worklaptop",
           {"fn": s_hostile, "hostile": True, "profile": "worklaptop"}),
    "S4": ("unchanged refresh", {"fn": s_refresh_unchanged, "hostile": False,
                                         "profile": "worklaptop"}),
    "S5": ("mutated refresh", {"fn": s_refresh_mutated, "hostile": False,
                                       "profile": "worklaptop"}),
    "S6": ("search storm during crawl", {"fn": s_search_storm,
                                         "hostile": False,
                                         "profile": "worklaptop"}),
    "S7": ("two IDE windows", {"fn": s_two_windows, "hostile": False,
                                       "profile": "worklaptop"}),
    "S8": ("cancel mid-crawl", {"fn": s_cancel, "hostile": False,
                                        "profile": "worklaptop"}),
    "S9": ("kill -9 + resume", {"fn": s_kill_resume, "hostile": False,
                                        "profile": "worklaptop"}),
    "S10": ("memory pressure pause/resume",
            {"fn": s_mem_pressure, "hostile": False, "profile": "worklaptop",
                 "env_extra": {"KNOWLEDGE_MIN_FREE_MB":
                               "{__free_minus_512__}"}}),
    "S11": ("idle unload", {"fn": s_idle_unload, "hostile": False,
                                "profile": "worklaptop",
                                "env_extra": {"KNOWLEDGE_IDLE_UNLOAD_S": "60"}}),
    "S12": ("battery saver", {"fn": s_battery, "hostile": False,
                                  "profile": "worklaptop",
                                  "env_extra": {"KNOWLEDGE_FORCE_BATTERY": "1"}}),
}

SCALE_SCENARIOS = ("S2", "S3", "S4")  # the ones repeated at 1000 pages


# ---------------------------------------------------------------------------
# report + main


def render_report(results: list[dict], out_dir: Path) -> Path:
    lines = ["# Confluence stress report",
             "",
             (f"Machine: {os.uname().machine}, {os.cpu_count()} cores; "
              "worklaptop profile = KNOWLEDGE_THREADS=2, 8-core budget."),
             "",
             ("| Scenario | Wall s | Peak RSS MB | CPU% p50 | Freezes | "
              "Stalls | Blind s | Wait1 s | Canary | Verdicts |"),
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        res, lap, fr = r.get("resources", {}), r.get("laptop", {}), \
            r.get("freezes", {})
        wall = (r.get("wall_s") or r.get("extra_wall_s")
                or r.get("refresh_wall_s") or "?")
        fails = [k for k, s in r["verdicts"].items() if s == "FAIL"]
        lines.append(
            f"| {r['scenario']} | {wall} | "
            f"{res.get('peak_rss_mb', '?')} | {res.get('cpu_pct_p50', '?')} | "
            f"{fr.get('freeze_events', '?')} | "
            f"{lap.get('stall_events', '?')} | "
            f"{r.get('progress_blind_s', '?')} | "
            f"{r.get('wait_first_change_s', '?')} | "
            f"{r.get('canary_ok', '?')}/{r.get('canary_total', '?')} | "
            f"{'; '.join(fails) if fails else 'all PASS'} |")
    lines.append("")
    rep = out_dir / "report.md"
    rep.write_text("\n".join(lines), encoding="utf-8")
    return rep


async def _run_one(name, fn, spec, profile, pages, seed, models,
                   out_dir, env_extra) -> dict:
    async with scenario_ctx(name, pages, seed, spec["hostile"], profile,
                            models, out_dir, env_extra) as ctx:
        try:
            vd = await fn(ctx)
        except Exception as e:  # noqa: BLE001 — report what we measured
            vd = {"scenario completed": "FAIL"}
            ctx.notes.append(f"scenario error: {type(e).__name__}: {e}")
        return finish(ctx, vd)


def main() -> int:
    # cp1252 consoles can't print arrow/check glyphs — degrade, never crash (#308)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scenarios", default="all",
                    help="comma list or 'all' or 'scale'")
    ap.add_argument("--profile", default="worklaptop")
    ap.add_argument("--models", default="/tmp/ckperf")
    ap.add_argument("--out", default="/tmp/ck-stress")
    ap.add_argument("--ac", action="store_true",
                    help="pin KNOWLEDGE_FORCE_BATTERY=0 so battery "
                         "saver never engages mid-run")
    args = ap.parse_args()

    models = Path(args.models)
    if not (models / "models" / _embed_dir_name()).exists():
        raise SystemExit(f"models not prepared under {models} "
                         "(need models/nomic-embed-text-v1.5)")

    wanted = ([s for s in SCENARIOS] if args.scenarios == "all"
              else list(SCALE_SCENARIOS) if args.scenarios == "scale"
              else args.scenarios.split(","))
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    results = []
    for name in wanted:
        if name not in SCENARIOS:
            print(f"skip unknown scenario {name}", flush=True)
            continue
        _desc, spec = SCENARIOS[name]
        spec = dict(spec)
        fn = spec.pop("fn")
        env_extra = spec.pop("env_extra", {})
        # S10 needs the guard threshold just under current free mem
        if "{__free_minus_512__}" in env_extra.values():
            import psutil
            free_mb = int(psutil.virtual_memory().available / 1048576)
            env_extra = {k: (str(max(256, free_mb - 512))
                             if v_ == "{__free_minus_512__}" else v_)
                         for k, v_ in env_extra.items()}
        profile = spec.pop("profile", args.profile)
        if args.ac:
            env_extra = {**env_extra, "KNOWLEDGE_FORCE_BATTERY": "0"}
        out_dir = out_root / f"{name.lower()}-{args.pages}p"
        print(f"== {name} {spec} → {out_dir}", flush=True)
        t0 = time.monotonic()

        try:
            res = asyncio.run(_run_one(name, fn, spec, profile, args.pages,
                                       args.seed, models, out_dir, env_extra))
        except Exception as e:  # noqa: BLE001 — one bad scenario ≠ all lost
            rf = out_dir / "result.json"
            res = (json.loads(rf.read_text(encoding="utf-8")) if rf.exists()
                   else {"scenario": name,
                         "error": f"{type(e).__name__}: {e}",
                         "verdicts": {"scenario completed": "FAIL"}})
            res.setdefault("verdicts", {}).setdefault(
                "teardown clean", "FAIL")
        res["total_wall_s"] = round(time.monotonic() - t0, 1)
        results.append(res)
        print(f"   {res['verdicts']}", flush=True)

    rep = render_report(results, out_root)
    print(f"\nreport: {rep}", flush=True)
    (out_root / "all-results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
