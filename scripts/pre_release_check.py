#!/usr/bin/env python3
"""#212 pre-release gate — ONE command a releaser runs before tagging.

Stages (fail loudly, non-zero exit, readable report on any miss):
  1. wheel integrity   — build each wheel → fresh venv → import resolves
                         from site-packages, doctor + selftest pass
  2. engine leg        — e2e_real_data.py: shared daemon + fake DC +
                         canary docs → crawl → evidence → citations →
                         abstention → feedback (always runs)
  2b. conv eval        — e2e_conversational_eval.py: question-intent
                         matrix through the real tool surface on real
                         models (skips cleanly without a model store)
  3. cloud leg         — e2e_cloud_real.py against the REAL Confluence
                         Cloud space — only when the cloud env file
                         exists (skipped cleanly on machines without
                         credentials; never faked)

Report: PRE_RELEASE_REPORT.md — per-stage verdict + key metrics.
Gate:   exit 0 only when every run stage is green.

  python scripts/pre_release_check.py [--pkg confluence-knowledge]
  CK_CLOUD_ENV=~/.config/agents/cloud.env  python ...   # + cloud leg
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKGS = ["confluence-knowledge", "local-knowledge", "sharepoint-knowledge"]
REPORT = REPO / "PRE_RELEASE_REPORT.md"


def run(cmd: list[str], cwd=None, env=None, timeout=900) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=cwd or REPO, env=env, check=False,
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


# Sentinel modules per package — the surface a dropped file would break
# (server/daemon = fastmcp+uvicorn chain; retrieval = undeclared-dep
# canary). Kept explicit, not walk_packages: optional-dep modules may
# legitimately be unimportable in a bare wheel.
MODULE_SENTINELS = {
    "confluence-knowledge": [
        "confluence_knowledge.server", "confluence_knowledge.daemon",
        "confluence_knowledge.crawl", "confluence_knowledge.index",
        "confluence_knowledge.retrieval", "confluence_knowledge.client",
        "confluence_knowledge.cli", "confluence_knowledge.rpc",
        "confluence_knowledge.watchdog"],
    "local-knowledge": [
        "local_knowledge.server_local", "local_knowledge.crawl_local",
        "local_knowledge.cli_local"],
    "sharepoint-knowledge": [
        "sharepoint_knowledge.server_sp", "sharepoint_knowledge.crawl_sp",
        "sharepoint_knowledge.cli_sp"],
}


def check_install(py: str, pkg: str,
                  modules: list[str] | None = None) -> tuple[bool, str]:
    """Post-install wheel assertions — import every sentinel module
    (missing-file / undeclared-dep defects) and run the console script
    (broken entry point). Returns (ok, detail)."""
    mods = modules if modules is not None else MODULE_SENTINELS.get(pkg, [])
    if mods:
        rc, out = run(
            [py, "-c",
             "import importlib\n" +
             "\n".join(f"importlib.import_module('{m}')" for m in mods)],
            timeout=300)
        if rc != 0:
            return False, f"module import failed: {out.strip()[-200:]}"
    rc, out = run([py, "-m", "pip", "show", "-f", pkg], timeout=60)
    if rc != 0 or "Name:" not in out:
        return False, "dist metadata unreadable"
    # console script — the entry point a user actually invokes
    exe = Path(py).parent / pkg
    rc, out = run([str(exe), "--help"], timeout=60)
    if rc != 0:
        return False, f"console script failed: {out.strip()[-200:]}"
    return True, f"{len(mods)} modules + entry point OK"


def wheel_stage(pkg: str, rows: list) -> bool:
    """Build → install into a fresh venv → module sweep + entry point
    + selftest (when the host model store exists)."""
    src = REPO / "mcp" / pkg
    if not src.exists():
        rows.append((f"{pkg}:wheel", "SKIP", "no package dir"))
        return True
    whl_dir = REPO / ".wheel-test"
    whl_dir.mkdir(exist_ok=True)
    for stale in whl_dir.glob(f"{pkg.replace('-', '_')}-*.whl"):
        stale.unlink()                       # don't match a stale wheel
    with tempfile.TemporaryDirectory() as td:
        # build from a COPY: setuptools regenerates the tracked egg-info
        # inside src/ as a side effect — the gate must leave the tree
        # pristine
        import shutil
        build_src = Path(td) / pkg
        shutil.copytree(src, build_src,
                        ignore=shutil.ignore_patterns(
                            "*.egg-info", "__pycache__", ".venv"))
        rc, out = run([sys.executable, "-m", "pip", "wheel", "-w",
                       str(whl_dir), "--no-deps", str(build_src)],
                      timeout=600)
        whl = next(whl_dir.glob(f"{pkg.replace('-', '_')}-*.whl"), None)
        if rc != 0 or whl is None:
            rows.append((f"{pkg}:wheel", "FAIL",
                         "build produced no wheel"))
            return False
        venv.create(Path(td) / "v", with_pip=True)
        # ensurepip's bundled pip trusts the compromised emSign
        # intermediates (GHSA-6q48-6c7w-9gw2) — secure the venv's
        # installer before it installs our wheel
        try:
            import certifi as _host_cert
            _cert = ["--cert", _host_cert.where()]
        except Exception:   # noqa: BLE001 — any certifi
            _cert = []   # failure → default bundle
        run([str(Path(td) / "v" / "bin" / "python"), "-m",
             "pip", "install", "--quiet", "-U",
             "pip>=26.2.2", "certifi>=2026.10.19", *_cert], timeout=300)
        pip = str(Path(td) / "v" / "bin" / "pip")
        py = str(Path(td) / "v" / "bin" / "python")
        rc, out = run([pip, "install", "--quiet", f"{whl}[models]"],
                      timeout=1200)
        if rc != 0:
            rows.append((f"{pkg}:wheel", "FAIL",
                         f"pip install failed: {out[-300:]}"))
            return False
        rc, out = run([py, "-c",
                       "import importlib.metadata as m, sys; "
                       "d = m.distribution('" + pkg.replace("-", "_") +
                       "'); assert d is not None, 'dist missing'; "
                       "print(d.locate_file(''))"],
                      timeout=60)
        from_site = "site-packages" in out
        if rc != 0 or not from_site:
            rows.append((f"{pkg}:wheel", "FAIL",
                         f"locate={out.strip()[-120:]}"))
            return False
        ok, detail = check_install(py, pkg)
        rows.append((f"{pkg}:wheel", "PASS" if ok else "FAIL",
                     f"{whl.name} — {detail}"))
        if not ok:
            return False
        # selftest — import_server + tools + health + mini_rag on real
        # models. Needs the host model store (~/.knowledge/models);
        # without it the leg can't run, never faked.
        models_home = (Path(os.environ.get(
            "KNOWLEDGE_SHARED_HOME", Path.home() / ".knowledge"))
            / "models")
        if models_home.is_dir() and any(models_home.iterdir()):
            rc, out = run([str(Path(py).parent / pkg), "selftest"],
                          timeout=900)
            ok = rc == 0
            rows.append((f"{pkg}:selftest", "PASS" if ok else "FAIL",
                         out.strip().splitlines()[-1][-160:]
                         if out.strip() else "no output"))
            return ok
        rows.append((f"{pkg}:selftest", "SKIP",
                     f"no models under {models_home}"))
        return True


def engine_stage(rows: list) -> bool:
    rc, out = run([sys.executable, str(REPO / "scripts" / "e2e_real_data.py")],
                  timeout=1800)
    ok = rc == 0 and "PASS" in out
    rows.append(("engine:e2e_real_data", "PASS" if ok else "FAIL",
                 "real-data journey (shared daemon, canary, citations)"))
    return ok


def conv_eval_stage(rows: list) -> bool:
    """#210 conversational eval — question-intent matrix through the
    real tool surface on real models. Skips cleanly without a model
    store; never faked."""
    models_home = (Path(os.environ.get(
        "KNOWLEDGE_SHARED_HOME", Path.home() / ".knowledge")) / "models")
    if not (models_home.is_dir() and any(models_home.iterdir())):
        rows.append(("conv:eval", "SKIP",
                     f"no models under {models_home}"))
        return True
    rc, out = run([sys.executable, str(
        REPO / "mcp" / "confluence-knowledge" / "scripts" /
        "e2e_conversational_eval.py"), "--report",
        str(REPO / ".wheel-test" / "conv_eval.md")], timeout=900)
    ok = rc == 0
    tail = [l for l in out.splitlines() if "verdict" in l
            or "recall" in l or "abstention" in l]
    rows.append(("conv:eval", "PASS" if ok else "FAIL",
                 " · ".join(tail[-2:])[:160] or "see conv_eval.md"))
    return ok


def cloud_stage(rows: list) -> bool:
    if not Path.home().joinpath(".config/agents/cloud.env").exists():
        rows.append(("cloud:e2e_cloud_real", "SKIP",
                     ("no ~/.config/agents/cloud.env — rerun on a machine "
                     "with the Cloud credential file")))
        return True
    # optional scope: CK_E2E_ROOT/CK_E2E_MIN_PAGES pin the crawl to a
    # subtree — the full 874-page corpus is a multi-hour soak; a ~100-page
    # subtree proves the journey in ~35 min. Unset → whole corpus.
    root = os.environ.get("CK_E2E_ROOT")
    cmd = [sys.executable, str(REPO / "mcp" / "confluence-knowledge" /
                               "scripts" / "e2e_cloud_real.py")]
    detail = "real Cloud journey (874-page corpus)"
    if root:
        cmd += ["--root-id", root]
        detail = f"real Cloud journey (scoped subtree {root})"
    min_p = os.environ.get("CK_E2E_MIN_PAGES")
    if min_p:
        cmd += ["--min-pages", min_p]
    rc, _out = run(cmd, timeout=3600)
    ok = rc == 0
    rows.append(("cloud:e2e_cloud_real", "PASS" if ok else "FAIL", detail))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pkg", default=None,
                    help="gate a single wheel (default: all three)")
    args = ap.parse_args()

    rows: list[tuple[str, str, str]] = []
    print("pre-release gate — #212\n" + "-" * 40)
    wheels = [args.pkg] if args.pkg else PKGS
    # wheels are independent subprocess builds — run them in parallel.
    # The legs stay SEQUENTIAL on purpose: both spawn ONNX-saturated
    # daemons; overlapping them would starve the cloud crawl of CPU and
    # turn a passing run into a spurious wait_for_crawl timeout.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(wheels)) as pool:
        list(pool.map(lambda p: wheel_stage(p, rows), wheels))
    engine_stage(rows)
    conv_eval_stage(rows)
    cloud_stage(rows)

    fails = [r for r in rows if r[1] == "FAIL"]
    lines = ["# Pre-release report — #212\n",
             f"| {'check':<26} | verdict | detail |", "|---|---|---|"]
    lines += [f"| {n} | {v} | {d} |" for n, v, d in rows]
    verdict = ("GREEN — safe to tag" if not fails
               else f"RED — {len(fails)} check(s) failed")
    lines += ["", f"**verdict: {verdict}**"]
    REPORT.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nreport → {REPORT}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
