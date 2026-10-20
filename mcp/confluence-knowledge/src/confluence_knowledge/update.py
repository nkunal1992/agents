"""Safe self-update — soak-aware release selection + snapshot/rollback.

One PyPI JSON GET per call (``KNOWLEDGE_UPDATE_INDEX`` overrides for
internal mirrors; ``KNOWLEDGE_UPDATE_SOAK_DAYS`` overrides the 3-day soak;
pip's own ``PIP_INDEX_URL``/``PIP_FIND_LINKS`` cover the install step).

`check()` — what doctor reports — never raises and never installs.

`run_update()` — what `<cli> update` runs:

  resolve target (newest soaked non-yanked stable; --latest skips the
  soak; --to pins exactly; --rollback restores the last snapshot)
  → ``pip freeze`` snapshot under ``<home>/rollback/``
  → ``pip install "<dist>[models]==<target>"`` (+ ``<engine><=soaked``
    bound for services that depend on the engine)
  → ``<cli> prepare-models`` (idempotent — a changed lock refetches)
  → ``selftest`` in a subprocess on the NEW code
  → any stage failing: ``pip install -r <snapshot>`` + prepare-models +
    selftest on the old code → "rolled back", exit 3
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from packaging.version import Version

INDEX = "https://pypi.org/pypi/{dist}/json"
ENV_INDEX = "KNOWLEDGE_UPDATE_INDEX"
ENV_SOAK = "KNOWLEDGE_UPDATE_SOAK_DAYS"
TIMEOUT = 5
SOAK_DAYS_DEFAULT = 3
MAX_SNAPSHOTS = 3


@dataclass
class Release:
    version: Version
    uploaded: datetime | None  # None = no timestamps (mirror) → counts soaked
    yanked: bool
    yanked_reason: str | None
    prerelease: bool

    @property
    def stable(self) -> bool:
        return not self.prerelease and not self.yanked


# ---- versions ----

def installed_version(dist: str) -> str | None:
    try:
        return version(dist)
    except PackageNotFoundError:
        return None


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)  # py≥3.11 handles "Z"
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _pypi(dist: str, timeout: float = TIMEOUT) -> dict | None:
    url = os.environ.get(ENV_INDEX, INDEX).format(dist=dist)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001 — every failure means "couldn't check"
        return None


def releases(dist: str, timeout: float = TIMEOUT) -> list[Release] | None:
    """Every release on the index, or None when it can't be checked."""
    data = _pypi(dist, timeout)
    if data is None:
        return None
    out = []
    for v, files in (data.get("releases") or {}).items():
        if not files:
            continue
        try:
            ver = Version(v)
        except Exception:  # noqa: BLE001,S112 — skip unparseable tags
            continue
        uploaded = min(
            (t for t in (_parse_ts(f.get("upload_time_iso_8601")
                                     or f.get("upload_time"))
                         for f in files) if t),
            default=None)
        reasons = {f.get("yanked_reason") for f in files if f.get("yanked")}
        out.append(Release(
            ver, uploaded, all(f.get("yanked") for f in files),
            "; ".join(sorted(reasons)) or None, ver.is_prerelease))
    return out


def latest_version(dist: str, timeout: float = TIMEOUT) -> str | None:
    """Newest stable, non-yanked release — None when uncheckable."""
    rels = releases(dist, timeout)
    if not rels:
        return None
    stable = [r.version for r in rels if r.stable]
    return str(max(stable)) if stable else None


def soak_days() -> int:
    try:
        return max(0, int(os.environ.get(ENV_SOAK, SOAK_DAYS_DEFAULT)))
    except ValueError:
        return SOAK_DAYS_DEFAULT


def _soaked(r: Release, now: datetime, days: int) -> bool:
    return r.uploaded is None or r.uploaded <= now - timedelta(days=days)


def _newest(rels: list[Release]) -> Version | None:
    stable = [r.version for r in rels if r.stable]
    return max(stable) if stable else None


def _newest_soaked(rels: list[Release], now: datetime) -> Version | None:
    days = soak_days()
    soaked = [r.version for r in rels if r.stable and _soaked(r, now, days)]
    return max(soaked) if soaked else None


def check(dist: str) -> dict:
    """{installed, latest, latest_soaked, pending, installed_yanked,
    update_available} — the dict doctor reports."""
    inst = installed_version(dist)
    rels = releases(dist) or []
    now = datetime.now(UTC)
    latest = _newest(rels)
    latest_soaked = _newest_soaked(rels, now)
    inst_rel = next((r for r in rels if inst and r.version == Version(inst)),
                    None)
    inst_yanked = bool(inst_rel and inst_rel.yanked)
    pending = None
    if latest and latest != latest_soaked:
        rel = next((r for r in rels if r.stable and r.version == latest),
                   None)
        pending = {"version": str(latest),
                   "offered_after": (rel.uploaded + timedelta(
                        days=soak_days())).date().isoformat()
                   if rel and rel.uploaded else "unknown"}
    target, _ = pick_target(rels, inst)
    return {
        "installed": inst,
        "latest": str(latest) if latest else None,
        "latest_soaked": str(latest_soaked) if latest_soaked else None,
        "pending": pending,
        "installed_yanked": inst_yanked,
        "yank_reason": inst_rel.yanked_reason if inst_rel else None,
        "update_available": target is not None,
    }


def pick_target(rels: list[Release] | None, installed: str | None,
                *, to: str | None = None, latest: bool = False,
                now: datetime | None = None) -> tuple[str | None, str]:
    """Which release `update` should install. Returns (version, reason)."""
    now = now or datetime.now(UTC)
    days = soak_days()
    rels = rels or []
    if to:
        rel = next((r for r in rels if r.version == Version(to)), None)
        if rels and rel is None:
            return None, f"{to} doesn't exist on the index"
        if rel and rel.yanked:
            return None, (f"{to} was yanked"
                          + (f" ({rel.yanked_reason})"
                             if rel.yanked_reason else "")
                          + " — refusing to install it")
        return to, f"pinned target {to}"
    inst_v = Version(installed) if installed else None
    inst_rel = next((r for r in rels if inst_v and r.version == inst_v), None)
    inst_yanked = bool(inst_rel and inst_rel.yanked)
    if inst_yanked:
        # move off the bad release — last-good soaked version (downgrade ok)
        target = _newest_soaked(rels, now)
        if target is None:
            return None, ("installed release was yanked and no soaked "
                          "replacement exists — retry later or --latest")
        return str(target), (f"installed {installed} was yanked — "
                             f"moving to last-good {target}")
    newest = _newest(rels)
    if latest:
        if newest is None:
            return None, "no releases found on the index"
        return str(newest), "newest release (--latest: soak skipped)"
    target = _newest_soaked(rels, now)
    if target is None:
        if newest is not None:
            return None, (f"newest release {newest} is still soaking "
                          f"({days}d) — offered when the soak passes")
        return None, "no releases found on the index"
    if inst_v and target <= inst_v:
        return None, f"up to date ({installed})"
    return str(target), f"newest release aged ≥{days}d"


# ---- update + rollback ----

def _run(argv: list[str], timeout: int = 1800) -> tuple[int, str]:
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       check=False)
    return r.returncode, (r.stdout + r.stderr).strip()


def _editable(dist: str) -> bool:
    try:
        from importlib.metadata import distribution
        raw = distribution(dist).read_text("direct_url.json") or "{}"
        return bool(json.loads(raw).get("dir_info", {}).get("editable"))
    except Exception:  # noqa: BLE001 — unknown = not editable
        return False


def _snapshot(home: Path, dist: str, installed: str | None,
              run=_run) -> Path | None:
    rc, out = run([sys.executable, "-m", "pip", "freeze"])
    if rc != 0 or not out.strip():
        return None
    rb = home / "rollback"
    rb.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snap = rb / f"{ts}-{installed or 'unknown'}.txt"
    snap.write_text(out)
    for old in sorted(rb.glob("*.txt"))[:-MAX_SNAPSHOTS]:
        old.unlink(missing_ok=True)
    (rb / "last.json").write_text(json.dumps(
        {"dist": dist, "from": installed, "snapshot": str(snap),
         "ts": ts}))
    return snap


def _restore_snapshot(home: Path) -> Path | None:
    last = home / "rollback" / "last.json"
    if not last.exists():
        return None
    try:
        return Path(json.loads(last.read_text())["snapshot"])
    except Exception:  # noqa: BLE001
        return None


def _selftest(home: Path, server_module: str, run=_run,
              timeout: int = 300) -> tuple[bool, str]:
    rc, out = run([sys.executable, "-m", "confluence_knowledge.selftest",
                   "--home", str(home), "--server-module", server_module],
                  timeout=timeout)
    return rc == 0, out


def _prepare(cli_module: str, run=_run) -> tuple[int, str]:
    return run([sys.executable, "-m", cli_module, "prepare-models"],
               timeout=3600)


def run_update(dist: str, *, cli_module: str, home: Path,
               server_module: str, to: str | None = None,
               latest: bool = False, rollback: bool = False,
               engine_dist: str | None = None,
               run=_run, rel_fetcher=releases) -> int:
    """Full safe-update flow. 0 = ok/up-to-date, 2 = refused, 3 = rolled back."""
    if _editable(dist):
        print(f"{dist} is a dev/editable install — update it via git, "
              "not pip.")
        return 2
    # a locked exe can't be replaced — the venv python can. Hand off.
    if os.name == "nt" and sys.argv[0].lower().endswith(".exe"):
        print("note: quit the IDE (or stop the MCP server) first — a "
              "running server can't be replaced cleanly", flush=True)
        print("on Windows, run the full safe update via the venv python:\n  "
              + " ".join([f'"{sys.executable}"', "-m", cli_module]
                         + sys.argv[1:]))
        return 0
    print("note: quit the IDE (or stop the MCP server) before updating —"
          " a running server can't be replaced cleanly", flush=True)

    installed = installed_version(dist)

    if rollback:
        snap = _restore_snapshot(home)
        if snap is None or not snap.exists():
            print("no rollback snapshot found — nothing to restore")
            return 2
        print(f"rolling back from {snap.name}")
        return _install_then_verify(
            ["-r", str(snap)], dist, cli_module, home, server_module,
            label=f"snapshot {snap.name}", prev=installed,
            take_snapshot=False, run=run)

    rels = rel_fetcher(dist)
    if rels is None:
        # index unreachable — soak can't apply; the rest of the safety net
        # (snapshot, self-test, rollback) still does
        if to is None and not latest:
            print("couldn't check the index — installing the newest "
                  "release pip sees (soak can't be applied; snapshot + "
                  "self-test + rollback still protect this update)")
            return _install_then_verify(
                [f"{dist}[models]"], dist, cli_module, home,
                server_module, label="latest", prev=installed, run=run)
        if to is not None:
            print("couldn't check the index — installing the pinned "
                  "target (yank status unchecked)")
    target, reason = pick_target(rels, installed, to=to, latest=latest)
    if target is None:
        print(reason)
        return 0

    bound = []
    if engine_dist and not latest:
        eng_rels = rel_fetcher(engine_dist)
        if eng_rels:
            now = datetime.now(UTC)
            soaked = _newest_soaked(eng_rels, now)
            bound = [f"{engine_dist}<={soaked}" if soaked
                     else f"{engine_dist}<0"]  # none soaked → resolve fails cleanly
        else:
            print(f"couldn't check {engine_dist} releases — "
                  "no engine bound applied")

    args = [f"{dist}[models]=={target}"] + bound
    print(f"update target: {target} — {reason}")
    return _install_then_verify(args, dist, cli_module, home,
                                server_module, label=target,
                                prev=installed, run=run)


def _install_then_verify(install_args: list[str], dist: str,
                         cli_module: str, home: Path, server_module: str,
                         *, label: str, prev: str | None = None,
                         take_snapshot: bool = True, run=_run) -> int:
    snap = None
    if take_snapshot:
        snap = _snapshot(home, dist, prev, run)
        if snap is None:
            print("couldn't snapshot the environment (pip freeze failed) "
                  "— refusing to update without a rollback path",
                  flush=True)
            return 2
    stages = [("install", [sys.executable, "-m", "pip", "install"]
               + install_args),
              ("prepare-models", None),  # via _prepare
              ("selftest", None)]        # via _selftest
    for name, argv in stages:
        if argv is not None:
            rc, out = run(argv)
            ok = rc == 0
        elif name == "prepare-models":
            rc, out = _prepare(cli_module, run)
            ok = rc == 0
        else:
            ok, out = _selftest(home, server_module, run)
        if not ok:
            print(f"update to {label} failed at {name}:\n{out[-2000:]}",
                  flush=True)
            if snap is not None:
                _do_rollback(snap, cli_module, home, server_module, run)
            else:
                print("no snapshot — nothing restored", flush=True)
            return 3
        print(f"  {name}: ok", flush=True)
    print(f"updated {prev or '?'} → {label}; self-test passed. "
          "Reload the IDE to run the new version.")
    return 0


def _do_rollback(snap: Path, cli_module: str, home: Path,
                 server_module: str, run=_run) -> None:
    print(f"restoring snapshot {snap.name} ...", flush=True)
    rc, out = run([sys.executable, "-m", "pip", "install", "-r", str(snap)])
    if rc != 0:
        print(f"WARNING: rollback install failed:\n{out[-2000:]}\n"
              f"restore manually: pip install -r {snap}", flush=True)
        return
    _prepare(cli_module, run)  # restores the previous pinned model revs
    ok, out = _selftest(home, server_module, run)
    print(("rolled back — previous version verified working"
           if ok else
           f"rolled back but the restored version fails self-test:\n{out}"),
          flush=True)
