"""Safe-update unit tests — release selection, soak, yank, rollback.

PyPI JSON, subprocess and the filesystem are all mocked — no network.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta

from packaging.version import Version

from confluence_knowledge import update
from confluence_knowledge.update import Release

NOW = datetime.now(UTC)


def _pypi(*specs):
    """Build fake PyPI JSON. Each spec: (version, days_ago, yanked) or str."""
    rels = {}
    for s in specs:
        v, days, yanked = (s if isinstance(s, tuple)
                           else (s, 10, False))
        ts = (NOW - timedelta(days=days)).isoformat()
        rels[v] = [{"filename": f"x-{v}.whl", "yanked": yanked,
                    "yanked_reason": "bad release" if yanked else None,
                    "upload_time_iso_8601": ts}]
    return {"info": {"version": "0"}, "releases": rels}


def _rel(v: str, days: float, yanked: bool = False) -> Release:
    return Release(Version(v), NOW - timedelta(days=days), yanked,
                   "bad release" if yanked else None, Version(v).is_prerelease)


def _fetcher(map_):
    def f(dist, timeout=5):
        return map_.get(dist)
    return f


# ---- releases(): parsing ----

def test_releases_parses(monkeypatch):
    monkeypatch.setattr(update, "_pypi",
                        lambda d, timeout=5: _pypi(("0.7.0", 10, False),
                                                 ("0.8.0rc1", 1, False),
                                                 ("0.6.9", 20, True)))
    rels = update.releases("x")
    by = {str(r.version): r for r in rels}
    assert by["0.7.0"].stable and not by["0.7.0"].yanked
    assert by["0.8.0rc1"].prerelease and not by["0.8.0rc1"].stable
    assert by["0.6.9"].yanked and by["0.6.9"].yanked_reason == "bad release"
    assert by["0.7.0"].uploaded is not None


def test_releases_offline(monkeypatch):
    monkeypatch.setattr(update, "_pypi", lambda d, timeout=5: None)
    assert update.releases("x") is None
    assert update.latest_version("x") is None


# ---- PEP 440 ordering (the old _version_tuple bug: rc1 > final) ----

def test_rc_orders_before_final():
    assert Version("0.7.0rc1") < Version("0.7.0")
    assert Version("0.7.0rc1") < Version("0.7.1")


def test_latest_skips_rc_and_yanked():
    rels = update.releases.__wrapped__("x") if False else None
    rels = [_rel("0.6.0", 30), _rel("0.7.0rc1", 1),
            _rel("0.6.5", 20, yanked=True)]
    assert str(update._newest(rels)) == "0.6.0"


# ---- pick_target ----

def test_target_newest_soaked():
    rels = [_rel("0.6.0", 30), _rel("0.7.0", 5), _rel("0.7.1", 1)]
    t, _ = update.pick_target(rels, "0.6.0", now=NOW)
    assert t == "0.7.0"


def test_target_none_when_only_unsoaked():
    rels = [_rel("0.6.0", 30), _rel("0.7.0", 1)]
    t, reason = update.pick_target(rels, "0.6.0", now=NOW)
    assert t is None and ("soaking" in reason or "up to date" in reason)


def test_target_latest_skips_soak():
    rels = [_rel("0.6.0", 30), _rel("0.7.0", 1)]
    t, _ = update.pick_target(rels, "0.6.0", latest=True, now=NOW)
    assert t == "0.7.0"


def test_target_up_to_date():
    rels = [_rel("0.6.0", 30), _rel("0.7.0", 10)]
    t, reason = update.pick_target(rels, "0.7.0", now=NOW)
    assert t is None and "up to date" in reason


def test_target_pinned_ok():
    rels = [_rel("0.6.0", 30), _rel("0.7.0", 1)]
    t, _ = update.pick_target(rels, "0.6.0", to="0.7.0", now=NOW)
    assert t == "0.7.0"


def test_target_pinned_missing():
    rels = [_rel("0.6.0", 30)]
    t, reason = update.pick_target(rels, "0.6.0", to="9.9.9", now=NOW)
    assert t is None and "doesn't exist" in reason


def test_target_pinned_yanked_refused():
    rels = [_rel("0.6.0", 30), _rel("0.7.0", 10, yanked=True)]
    t, reason = update.pick_target(rels, "0.6.0", to="0.7.0", now=NOW)
    assert t is None and "yanked" in reason


def test_target_installed_yanked_downgrades():
    # installed 0.7.0 was yanked → last-good soaked is 0.6.0 (a downgrade)
    rels = [_rel("0.6.0", 30), _rel("0.7.0", 10, yanked=True)]
    t, reason = update.pick_target(rels, "0.7.0", now=NOW)
    assert t == "0.6.0" and "yanked" in reason


def test_target_rc_never_offered():
    rels = [_rel("0.6.0", 30), _rel("0.7.0rc1", 10)]
    t, _ = update.pick_target(rels, "0.6.0", now=NOW)
    assert t == "0.6.0" or t is None  # rc isn't a valid target
    assert t != "0.7.0rc1"


# ---- check() ----

def test_check_reports_pending_and_soak(monkeypatch):
    f = _fetcher({"x": [_rel("0.6.0", 30), _rel("0.7.0", 1)]})
    monkeypatch.setattr(update, "releases", f)
    monkeypatch.setattr(update, "installed_version", lambda d: "0.6.0")
    r = update.check("x")
    assert r["latest"] == "0.7.0" and r["latest_soaked"] == "0.6.0"
    assert r["pending"]["version"] == "0.7.0"
    assert r["update_available"] is False


def test_check_update_available_after_soak(monkeypatch):
    f = _fetcher({"x": [_rel("0.6.0", 30), _rel("0.7.0", 10)]})
    monkeypatch.setattr(update, "releases", f)
    monkeypatch.setattr(update, "installed_version", lambda d: "0.6.0")
    r = update.check("x")
    assert r["latest_soaked"] == "0.7.0" and r["update_available"] is True


def test_check_installed_yanked(monkeypatch):
    f = _fetcher({"x": [_rel("0.6.0", 30),
                        _rel("0.7.0", 10, yanked=True)]})
    monkeypatch.setattr(update, "releases", f)
    monkeypatch.setattr(update, "installed_version", lambda d: "0.7.0")
    r = update.check("x")
    assert r["installed_yanked"] is True
    assert r["yank_reason"] == "bad release"
    assert r["update_available"] is True  # last-good target exists


def test_check_offline_silent(monkeypatch):
    monkeypatch.setattr(update, "releases", lambda d, timeout=5: None)
    r = update.check("x")
    assert r["latest"] is None and r["update_available"] is False


# ---- run_update: the flow ----

class FakeRun:
    """Record argv; script outcomes by argv content."""

    def __init__(self, fail_at: str | None = None):
        self.calls: list[list[str]] = []
        self.fail_at = fail_at

    def __call__(self, argv, timeout=1800):
        self.calls.append(argv)
        joined = " ".join(map(str, argv))
        if self.fail_at and self.fail_at in joined:
            return 1, f"FAILED {self.fail_at}"
        if "freeze" in joined:
            return 0, "confluence-knowledge==0.6.0\nnumpy==2.0.0"
        return 0, "ok"


def _update(run, tmp_path, **kw):
    kw.setdefault("run", run)
    return update.run_update(
        "confluence-knowledge", cli_module="confluence_knowledge.cli",
        home=tmp_path, server_module="confluence_knowledge.server",
        **kw)


def test_update_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda d: "0.6.0")
    monkeypatch.setattr(update, "_editable", lambda d: False)
    f = _fetcher({"confluence-knowledge":
                  [_rel("0.6.0", 30), _rel("0.7.0", 10)]})
    run = FakeRun()
    rc = _update(run, tmp_path, rel_fetcher=f)
    assert rc == 0
    joined = [" ".join(map(str, c)) for c in run.calls]
    assert any("freeze" in j for j in joined)
    assert any("confluence-knowledge[models]==0.7.0" in j for j in joined)
    assert any("prepare-models" in j for j in joined)
    assert any("selftest" in j for j in joined)
    # snapshot written
    assert (tmp_path / "rollback" / "last.json").exists()


def test_update_failure_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda d: "0.6.0")
    monkeypatch.setattr(update, "_editable", lambda d: False)
    f = _fetcher({"confluence-knowledge":
                  [_rel("0.6.0", 30), _rel("0.7.0", 10)]})
    run = FakeRun(fail_at="selftest")
    rc = _update(run, tmp_path, rel_fetcher=f)
    assert rc == 3
    # rollback ran pip install -r <snapshot>
    joined = [" ".join(map(str, c)) for c in run.calls]
    assert any(" -r " in j and "rollback" in j for j in joined)


def test_update_refuses_editable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(update, "_editable", lambda d: True)
    run = FakeRun()
    assert _update(run, tmp_path) == 2
    assert "dev/editable" in capsys.readouterr().out
    assert not run.calls


def test_update_rollback_restores_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda d: "0.7.0")
    monkeypatch.setattr(update, "_editable", lambda d: False)
    rb = tmp_path / "rollback"
    rb.mkdir(parents=True)
    snap = rb / "x-0.6.0.txt"
    snap.write_text("confluence-knowledge==0.6.0\nnumpy==2.0.0")
    (rb / "last.json").write_text(json.dumps(
        {"dist": "x", "from": "0.7.0", "snapshot": str(snap), "ts": "t"}))
    run = FakeRun()
    rc = _update(run, tmp_path, rollback=True)
    assert rc == 0
    joined = [" ".join(map(str, c)) for c in run.calls]
    assert any(" -r " in j and str(snap) in j for j in joined)


def test_update_rollback_without_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "_editable", lambda d: False)
    assert _update(FakeRun(), tmp_path, rollback=True) == 2


def test_update_up_to_date_no_install(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda d: "0.7.0")
    monkeypatch.setattr(update, "_editable", lambda d: False)
    f = _fetcher({"confluence-knowledge":
                  [_rel("0.6.0", 30), _rel("0.7.0", 10)]})
    run = FakeRun()
    rc = _update(run, tmp_path, rel_fetcher=f)
    assert rc == 0
    assert not any("install" in " ".join(map(str, c)) for c in run.calls)


def test_update_engine_bound_for_dependents(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda d: "0.3.0")
    monkeypatch.setattr(update, "_editable", lambda d: False)
    f = _fetcher({"local-knowledge": [_rel("0.3.0", 30), _rel("0.4.0", 10)],
                  "confluence-knowledge": [_rel("0.6.0", 30),
                                           _rel("0.7.0", 10),
                                           _rel("0.7.1", 1)]})
    run = FakeRun()
    rc = update.run_update(
        "local-knowledge", cli_module="local_knowledge.cli_local",
        home=tmp_path, server_module="local_knowledge.server_local",
        engine_dist="confluence-knowledge", rel_fetcher=f, run=run)
    assert rc == 0
    joined = " ".join(" ".join(map(str, c)) for c in run.calls)
    assert "local-knowledge[models]==0.4.0" in joined
    assert "confluence-knowledge<=0.7.0" in joined  # 0.7.1 still soaking


def test_update_offline_falls_back_unpinned(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "installed_version", lambda d: "0.6.0")
    monkeypatch.setattr(update, "_editable", lambda d: False)
    run = FakeRun()
    rc = _update(run, tmp_path, rel_fetcher=lambda d: None)
    assert rc == 0
    joined = [" ".join(map(str, c)) for c in run.calls]
    # unpinned install (no ==version) — pip resolves
    assert any("confluence-knowledge[models]" in j and "==" not in j
               for j in joined)


def test_update_snapshot_retention(tmp_path, monkeypatch):
    rb = tmp_path / "rollback"
    rb.mkdir(parents=True)
    for i in range(5):
        (rb / f"2025010{i}T000000Z-0.5.0.txt").write_text("x==1")
    monkeypatch.setattr(update, "installed_version", lambda d: "0.6.0")
    monkeypatch.setattr(update, "_editable", lambda d: False)
    f = _fetcher({"confluence-knowledge":
                  [_rel("0.6.0", 30), _rel("0.7.0", 10)]})
    run = FakeRun()
    _update(run, tmp_path, rel_fetcher=f)
    assert len(list(rb.glob("*.txt"))) == update.MAX_SNAPSHOTS


def test_update_windows_exe_handsoff(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(update, "_editable", lambda d: False)
    monkeypatch.setattr(update.os, "name", "nt")
    monkeypatch.setattr(sys, "argv",
                        ["C:\\venv\\Scripts\\confluence-knowledge.exe",
                         "update", "--latest"])
    run = FakeRun()
    rc = _update(run, tmp_path)
    assert rc == 0
    out = capsys.readouterr().out
    assert "-m confluence_knowledge.cli update --latest" in out
    assert not run.calls  # nothing installed in-process
