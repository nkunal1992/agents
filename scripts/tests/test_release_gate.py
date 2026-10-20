"""Unit tests for scripts/release_gate.py — all gate rules, fully mocked."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_gate as rg

REPO = Path(__file__).resolve().parents[2]
SHA = "a" * 40
VER = "9.9.9"  # synthetic target — never exists on the fake PyPI


def _root(tmp_path: Path, pkg: str, version: str = VER,
          deps: tuple = ()) -> Path:
    d = tmp_path / "mcp" / pkg
    d.mkdir(parents=True)
    (d / "pyproject.toml").write_text(
        """[project]\nname = "{}"\nversion = "{}"\ndependencies = [{}]\n""".format(
            pkg, version, ", ".join(f'"{x}"' for x in deps)))
    return tmp_path


def _pypi(*versions: str, yanked: tuple = ()):
    rels = {}
    for v in versions:
        rels[v] = [{"filename": f"x-{v}.whl", "yanked": v in yanked,
                    "upload_time_iso_8601": "2026-01-01T00:00:00"}]
    return {"info": {"version": versions[-1] if versions else "0"},
            "releases": rels}


def _fetcher(map_: dict):
    return map_.get


def _runner_ok(*_argv):
    return 0, ""


def _runner_no_ancestor(*argv):
    return (1, "") if argv[0] == "git" else (0, "")


def _rc_runs(sha: str, branch: str, conclusion: str):
    def r(*argv):
        if argv[0] == "gh":
            return 0, json.dumps([{"headSha": sha, "headBranch": branch,
                                   "conclusion": conclusion}])
        return 0, ""
    return r


CONF = {"confluence-knowledge": _pypi("0.5.4", "0.6.0")}


# ---- tag parsing ----

def test_parse_tag_valid():
    assert rg.parse_tag("confluence-knowledge-v0.7.0") == \
        ("confluence-knowledge", "0.7.0", None)
    assert rg.parse_tag("local-knowledge-v0.4.0rc1") == \
        ("local-knowledge", "0.4.0", "rc1")


@pytest.mark.parametrize("tag", [
    "code-reviewer-v1.0.0", "confluence-knowledge-v0.7",
    "confluence-knowledge-0.7.0", "v0.7.0",
    "confluence-knowledge-v0.7.0-beta", "confluence-knowledge-v0.7.0rc0",
])
def test_parse_tag_rejects(tag):
    with pytest.raises(rg.GateError):
        rg.parse_tag(tag) if "rc0" not in tag else \
            rg.validate(tag, SHA, fetcher=_fetcher(CONF),
                        runner=_runner_ok, root=REPO)


# ---- validate: happy paths ----

def test_rc_tag_passes_without_prior_rc_run(tmp_path):
    root = _root(tmp_path, "confluence-knowledge")
    out = rg.validate(f"confluence-knowledge-v{VER}rc1", SHA,
                      fetcher=_fetcher(CONF), runner=_runner_ok, root=root)
    assert out == {"pkg": "confluence-knowledge",
                   "version": f"{VER}rc1", "base": VER, "is_rc": True}


def test_final_tag_passes_with_green_rc_on_same_sha(tmp_path):
    root = _root(tmp_path, "confluence-knowledge")
    f = _fetcher({"confluence-knowledge":
                  _pypi("0.6.0", f"{VER}rc1")})
    r = _rc_runs(SHA, f"confluence-knowledge-v{VER}rc1", "success")
    out = rg.validate(f"confluence-knowledge-v{VER}", SHA,
                      fetcher=f, runner=r, root=root)
    assert out["is_rc"] is False


def test_final_fails_without_rc_run(tmp_path):
    root = _root(tmp_path, "confluence-knowledge")
    f = _fetcher({"confluence-knowledge": _pypi("0.6.0", f"{VER}rc1")})
    with pytest.raises(rg.GateError, match="no green RC"):
        rg.validate(f"confluence-knowledge-v{VER}", SHA, fetcher=f,
                    runner=_runner_ok, root=root)


def test_final_fails_when_rc_passed_on_different_sha(tmp_path):
    root = _root(tmp_path, "confluence-knowledge")
    f = _fetcher({"confluence-knowledge": _pypi("0.6.0", f"{VER}rc1")})
    r = _rc_runs("b" * 40, f"confluence-knowledge-v{VER}rc1", "success")
    with pytest.raises(rg.GateError, match="no green RC"):
        rg.validate(f"confluence-knowledge-v{VER}", SHA, fetcher=f,
                    runner=r, root=root)


def test_final_fails_when_rc_run_failed(tmp_path):
    root = _root(tmp_path, "confluence-knowledge")
    f = _fetcher({"confluence-knowledge": _pypi("0.6.0", f"{VER}rc1")})
    r = _rc_runs(SHA, f"confluence-knowledge-v{VER}rc1", "failure")
    with pytest.raises(rg.GateError, match="no green RC"):
        rg.validate(f"confluence-knowledge-v{VER}", SHA, fetcher=f,
                    runner=r, root=root)


# ---- validate: version / pyproject / main rules ----

def test_tag_pyproject_mismatch(tmp_path):
    root = _root(tmp_path, "confluence-knowledge", version="0.7.0")
    with pytest.raises(rg.GateError, match="!= pyproject"):
        rg.validate("confluence-knowledge-v0.8.0rc1", SHA,
                    fetcher=_fetcher(CONF), runner=_runner_ok, root=root)


def test_commit_not_on_main(tmp_path):
    root = _root(tmp_path, "confluence-knowledge")
    with pytest.raises(rg.GateError, match="not on"):
        rg.validate(f"confluence-knowledge-v{VER}rc1", SHA,
                    fetcher=_fetcher(CONF), runner=_runner_no_ancestor,
                    root=root)


def test_duplicate_version_refused(tmp_path):
    root = _root(tmp_path, "confluence-knowledge")
    f = _fetcher({"confluence-knowledge": _pypi("0.6.0", f"{VER}rc1")})
    with pytest.raises(rg.GateError, match="already exists"):
        rg.validate(f"confluence-knowledge-v{VER}rc1", SHA, fetcher=f,
                    runner=_runner_ok, root=root)


def test_backwards_version_refused(tmp_path):
    root = _root(tmp_path, "confluence-knowledge", version="0.5.0")
    with pytest.raises(rg.GateError, match="not newer"):
        rg.validate("confluence-knowledge-v0.5.0", SHA,
                    fetcher=_fetcher(CONF), runner=_runner_ok, root=root)


# ---- local/SP engine-pin rule ----

def test_local_tag_needs_matching_engine(tmp_path):
    root = _root(tmp_path, "local-knowledge",
                 deps=('confluence-knowledge~=0.7.0',))
    f = _fetcher({"local-knowledge": _pypi("0.3.0"),
                  "confluence-knowledge": _pypi("0.6.0")})
    with pytest.raises(rg.GateError, match="no released"):
        rg.validate(f"local-knowledge-v{VER}rc1", SHA, fetcher=f,
                    runner=_runner_ok, root=root)


def test_local_tag_passes_when_engine_released(tmp_path):
    root = _root(tmp_path, "local-knowledge",
                 deps=('confluence-knowledge~=0.7.0',))
    f = _fetcher({"local-knowledge": _pypi("0.3.0"),
                  "confluence-knowledge": _pypi("0.6.0", "0.7.0")})
    out = rg.validate(f"local-knowledge-v{VER}rc1", SHA, fetcher=f,
                      runner=_runner_ok, root=root)
    assert out["pkg"] == "local-knowledge"


def test_engine_specifier_parsed():
    s = rg.engine_specifier("local-knowledge")
    assert "0.6.0" in str(s) or "~=" in str(s)


# ---- set-version ----

def test_set_version_rewrites(tmp_path):
    root = _root(tmp_path, "confluence-knowledge")
    rg.set_version("confluence-knowledge", f"{VER}rc1", root=root)
    pp = (root / "mcp" / "confluence-knowledge" / "pyproject.toml")
    assert f'version = "{VER}rc1"' in pp.read_text()


def test_main_cli_rc_and_fail(tmp_path, monkeypatch, capsys):
    _root(tmp_path, "confluence-knowledge")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rg, "fetch_pypi", _fetcher(CONF).__call__)
    monkeypatch.setattr(rg, "run_cmd", _runner_ok)
    monkeypatch.setattr(rg, "REPO_ROOT", tmp_path)
    assert rg.main(["validate", "--tag",
                    f"confluence-knowledge-v{VER}rc1",
                    "--sha", SHA]) == 0
    assert "is_rc=True" in capsys.readouterr().out
    assert rg.main(["validate", "--tag", "bogus-tag", "--sha", SHA]) == 1
    assert "GATE FAIL" in capsys.readouterr().err
