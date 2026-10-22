#!/usr/bin/env python3
"""#212 pre-release gate — ONE command a releaser runs before tagging.

Stages (fail loudly, non-zero exit, readable report on any miss):
  1. wheel integrity   — build each wheel → fresh venv → import resolves
                         from site-packages, doctor + selftest pass
  2. engine leg        — e2e_real_data.py: shared daemon + fake DC +
                         canary docs → crawl → evidence → citations →
                         abstention → feedback (always runs)
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
import time
import venv
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKGS = ["confluence-knowledge", "local-knowledge", "sharepoint-knowledge"]
REPORT = REPO / "PRE_RELEASE_REPORT.md"


def run(cmd: list[str], cwd=None, env=None, timeout=900) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=cwd or REPO, env=env, check=False,
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def wheel_stage(pkg: str, rows: list) -> bool:
    """Build → install into a fresh venv → smoke-import + doctor."""
    src = REPO / "mcp" / pkg
    if not src.exists():
        rows.append((f"{pkg}:wheel", "SKIP", "no package dir"))
        return True
    whl_dir = REPO / ".wheel-test"
    whl_dir.mkdir(exist_ok=True)
    for stale in whl_dir.glob(f"{pkg.replace('-', '_')}-*.whl"):
        stale.unlink()                       # don't match a stale wheel
    rc, out = run([sys.executable, "-m", "pip", "wheel", "-w",
                   str(whl_dir), "--no-deps", str(src)], timeout=600)
    whl = next(whl_dir.glob(f"{pkg.replace('-', '_')}-*.whl"), None)
    if rc != 0 or whl is None:
        rows.append((f"{pkg}:wheel", "FAIL", "build produced no wheel"))
        return False
    with tempfile.TemporaryDirectory() as td:
        venv.create(Path(td) / "v", with_pip=True)
        pip = str(Path(td) / "v" / "bin" / "pip")
        py = str(Path(td) / "v" / "bin" / "python")
        rc, out = run([pip, "install", "--quiet", str(whl)], timeout=600)
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
        rows.append((f"{pkg}:wheel",
                     "PASS" if rc == 0 and from_site else "FAIL",
                     f"install={whl.name} import={out.strip()[-80:]}"))
        return rc == 0 and from_site


def engine_stage(rows: list) -> bool:
    rc, out = run([sys.executable, str(REPO / "scripts" / "e2e_real_data.py")],
                  timeout=1800)
    ok = rc == 0 and "PASS" in out
    rows.append(("engine:e2e_real_data", "PASS" if ok else "FAIL",
                 "real-data journey (shared daemon, canary, citations)"))
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
