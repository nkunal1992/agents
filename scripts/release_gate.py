#!/usr/bin/env python3
"""Gate checks for publish-pypi.yml — single place, unit-tested.

`validate` refuses a publish unless:
  1. the tag matches `<pkg>-vX.Y.Z[rcN]` and pyproject `version` == X.Y.Z
     (the base version — CI injects the rc suffix at build time),
  2. the tagged commit is an ancestor of `origin/main`,
  3. the version doesn't already exist on PyPI and is the newest,
  4. for a FINAL tag: a previous successful publish-pypi run exists for a
     `<pkg>-v<base>rc<N>` tag on the SAME commit (the RC was verified by
     installing from real PyPI — pip ignores pre-releases so users can't
     get it),
  5. for local-/sharepoint-knowledge: their `confluence-knowledge`
     requirement is satisfiable by a released, non-yanked, stable engine
     version on PyPI.

`set-version` rewrites the pyproject version line (RC builds only) so the
built artifact carries the tag's version.

Stdlib only except `packaging` (installed in the workflow). Everything
network/subprocess-backed goes through injectable functions for tests.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

try:
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
except ImportError:  # pragma: no cover — workflow installs packaging
    sys.exit("release_gate needs `packaging` — pip install packaging")

REPO_ROOT = Path(__file__).resolve().parents[1]
PKGS = ("confluence-knowledge", "local-knowledge", "sharepoint-knowledge")
ENGINE = "confluence-knowledge"
TAG_RE = re.compile(
    r"^(confluence|local|sharepoint)-knowledge-v"
    r"(\d+\.\d+\.\d+)(rc\d+)?$")
PYPI = "https://pypi.org/pypi/{dist}/json"
WORKFLOW = "publish-pypi.yml"


class GateError(Exception):
    """A gate check failed — printed as `GATE FAIL: <msg>`, exit 1."""


@dataclass
class Release:
    version: Version
    yanked: bool


# ---- injectable seams ----

def fetch_pypi(dist: str) -> dict:
    with urllib.request.urlopen(PYPI.format(dist=dist), timeout=15) as r:
        return json.loads(r.read())


def run_cmd(*argv: str) -> tuple[int, str]:
    r = subprocess.run(argv, capture_output=True, text=True, check=False)
    return r.returncode, r.stdout


# ---- checks ----

def parse_tag(tag: str) -> tuple[str, str, str | None]:
    m = TAG_RE.fullmatch(tag)
    if not m:
        raise GateError(
            f"tag {tag!r} doesn't match <pkg>-vX.Y.Z[rcN] "
            f"(pkg in {', '.join(PKGS)})")
    return m.group(1) + "-knowledge", m.group(2), m.group(3)


def pyproject_version(pkg: str, root: Path | None = None) -> str:
    root = root if root is not None else REPO_ROOT
    pp = root / "mcp" / pkg / "pyproject.toml"
    if not pp.exists():
        raise GateError(f"{pp} not found")
    return tomllib.loads(pp.read_text(encoding="utf-8"))["project"]["version"]


def engine_specifier(pkg: str, root: Path | None = None) -> SpecifierSet:
    """The `confluence-knowledge…` requirement inside a service's deps."""
    root = root if root is not None else REPO_ROOT
    deps = tomllib.loads(
        (root / "mcp" / pkg / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["dependencies"]
    for dep in deps:
        if dep.startswith(ENGINE):
            # "confluence-knowledge[files]~=0.7.0" → "~=0.7.0"
            return SpecifierSet(dep[len(ENGINE):].split("]")[-1].strip())
    raise GateError(f"{pkg} doesn't depend on {ENGINE}")


def releases(dist: str, fetcher: Callable | None = None) -> list[Release]:
    fetcher = fetcher or fetch_pypi
    out = []
    for v, files in (fetcher(dist).get("releases") or {}).items():
        try:
            ver = Version(v)
        except ValueError:
            continue
        if not files:
            continue  # no artifacts — can't be installed
        out.append(Release(ver, all(f.get("yanked") for f in files)))
    return out


def on_main(sha: str, runner: Callable | None = None, branch: str = "origin/main") -> None:
    runner = runner or run_cmd
    rc, _ = runner("git", "merge-base", "--is-ancestor", sha, branch)
    if rc != 0:
        raise GateError(
            f"{sha[:12]} is not on {branch} — publish only from main")


def check_version(pkg: str, version: str, rels: list[Release]) -> Version:
    v = Version(version)
    existing = {r.version for r in rels}
    if v in existing:
        raise GateError(f"{pkg} {v} already exists on PyPI — bump it")
    if rels and v <= max(existing):
        raise GateError(f"{pkg} {v} is not newer than {max(existing)}")
    return v


def check_rc_passed(pkg: str, base: str, sha: str,
                    runner: Callable | None = None) -> None:
    """A final tag requires a green RC run on the SAME commit."""
    runner = runner or run_cmd
    rc, out = runner("gh", "run", "list", "--workflow", WORKFLOW,
                     "--limit", "100", "--json",
                     "headSha,headBranch,conclusion")
    if rc != 0:
        raise GateError("couldn't query prior runs — gh CLI required")
    pat = re.compile(re.escape(f"{pkg}-v{base}") + r"rc\d+$")
    for r in json.loads(out or "[]"):
        if (r.get("headSha", "").startswith(sha[:12])
                and pat.match(r.get("headBranch") or "")
                and r.get("conclusion") == "success"):
            return
    raise GateError(
        f"no green RC run for {pkg} {base} on {sha[:12]} — tag "
        f"{pkg}-v{base}rc1 first and let it verify from PyPI")


def check_engine_pin(pkg: str, fetcher: Callable | None = None,
                     root: Path | None = None) -> None:
    root = root if root is not None else REPO_ROOT
    fetcher = fetcher or fetch_pypi
    spec = engine_specifier(pkg, root)
    ok = [r.version for r in releases(ENGINE, fetcher)
          if not r.yanked and not r.version.is_prerelease
          and spec.contains(r.version, prereleases=False)]
    if not ok:
        raise GateError(
            f"{pkg} requires {ENGINE}{spec} — no released, non-yanked "
            "engine version satisfies it (publish the engine final first)")


def validate(tag: str, sha: str, *, fetcher: Callable | None = None, runner: Callable | None = None,
             root: Path | None = None, main_branch: str = "origin/main"
             ) -> dict:
    fetcher = fetcher or fetch_pypi
    runner = runner or run_cmd
    root = root if root is not None else REPO_ROOT
    pkg, base, rc = parse_tag(tag)
    if rc is not None and int(rc[2:]) < 1:
        raise GateError("rc numbering starts at rc1")
    proj = pyproject_version(pkg, root)
    if proj != base:
        raise GateError(
            f"tag base {base} != pyproject version {proj} in mcp/{pkg}")
    on_main(sha, runner, main_branch)
    rels = releases(pkg, fetcher)
    check_version(pkg, base + (rc or ""), rels)
    if rc is None:
        check_rc_passed(pkg, base, sha, runner)
    if pkg != ENGINE:
        check_engine_pin(pkg, fetcher, root)
    return {"pkg": pkg, "version": base + (rc or ""),
            "base": base, "is_rc": bool(rc)}


def set_version(pkg: str, version: str, root: Path | None = None) -> Path:
    """Rewrite `version = "…"` in the package pyproject — exactly one hit."""
    root = root if root is not None else REPO_ROOT
    pp = root / "mcp" / pkg / "pyproject.toml"
    text = pp.read_text(encoding="utf-8")
    hits = re.findall(r'^version = "[^"]+"', text, flags=re.MULTILINE)
    if len(hits) != 1:
        raise GateError(
            f"{pp}: expected one `version =` line, found {len(hits)}")
    pp.write_text(re.sub(r'^version = "[^"]+"', f'version = "{version}"',
                         text, count=1, flags=re.MULTILINE), encoding="utf-8")
    return pp


def _emit_outputs(d: dict) -> None:
    lines = "".join(f"{k}={v}\n" for k, v in d.items())
    print(lines, end="")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="release_gate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--tag", required=True)
    v.add_argument("--sha", required=True)
    s = sub.add_parser("set-version")
    s.add_argument("--pkg", required=True)
    s.add_argument("--version", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "validate":
            _emit_outputs(validate(args.tag, args.sha))
        else:
            p = set_version(args.pkg, args.version)
            print(f"set {p} version = {args.version}")
    except GateError as e:
        print(f"GATE FAIL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
