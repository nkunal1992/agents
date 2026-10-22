"""Failure-mode e2e for confluence-knowledge — breaks a healthy install and
verifies every doctor flag + recovery path:

    partial model dir → model_embed flag → prepare-models → green
    corrupt index.db → "corrupt" flag + SetupError (not sqlite) → recover
    revoked token (fake 401) → error names `setup`
    register --remove → registration flag → register → green

Run: python scripts/e2e_failure.py   (set CK_MODEL_CACHE to pre-seed)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import atexit

from _e2e_common import check, serve, stop_shared_daemon, fresh_venv

REPO_SVC = Path(__file__).resolve().parents[1]


def run(argv, env, ok_msg=None, check_rc=True):
    p = subprocess.run(argv, check=False, env=env, capture_output=True,
                       text=True, encoding="utf-8",
                       errors="replace", timeout=600)
    if check_rc and p.returncode != 0:
        print(f"FAIL: {' '.join(map(str, argv))}\n{p.stdout}\n{p.stderr}",
              flush=True)
        os._exit(1)
    if ok_msg:
        print("PASS", ok_msg)
    return p


def main() -> None:
    srv, base, _hits = serve()
    sandbox = Path(tempfile.mkdtemp(prefix="ck-fail-")).resolve()
    home = sandbox / "home" / ".confluence-knowledge"
    work = sandbox / "work"
    home.mkdir(parents=True)
    work.mkdir()
    env = {**os.environ, "HOME": str(sandbox / "home"),
           "USERPROFILE": str(sandbox / "home"),
           "CONF_KNOWLEDGE_HOME": str(home),
           "CONF_KNOWLEDGE_WORKDIR": str(work),
           "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    print(f"sandbox: {sandbox}  fake: {base}")

    venv_dir = home / "venv"
    fresh_venv(venv_dir)
    bindir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    vpy = bindir / ("python.exe" if os.name == "nt" else "python")
    cli = bindir / ("confluence-knowledge.exe" if os.name == "nt"
                    else "confluence-knowledge")
    atexit.register(stop_shared_daemon, cli, env)
    spec = sys.argv[sys.argv.index("--spec") + 1] \
        if "--spec" in sys.argv else str(REPO_SVC) + "[models]"
    run([str(vpy), "-m", "pip", "install", spec], env,
        "install")

    cache = os.environ.get("CK_MODEL_CACHE")
    cache = os.path.expanduser(cache) if cache else None
    # #206 — models live in the SHARED store under the sandboxed HOME
    mdl = Path(env["HOME"]) / ".knowledge" / "models"
    if cache and (Path(cache) / "nomic-embed-text-v1.5").exists():
        shutil.copytree(cache, mdl, dirs_exist_ok=True)
        print("PASS models seeded from cache")
    else:
        run([str(cli), "prepare-models"], env, "prepare-models")

    # ---- break 1: partial model dir ------------------------------------
    onnx = (mdl / "nomic-embed-text-v1.5" / "tokenizer_config.json")
    onnx.unlink()
    d = run([str(cli), "doctor"], env, check_rc=False)
    check("model_embed" in d.stdout and "prepare-models" in d.stdout,
          "partial model → model_embed flag + remediation", d.stdout)
    check(d.returncode != 0, "doctor exits non-zero on red flag", d.returncode)
    run([str(cli), "prepare-models"], env, "remediation: re-fetch model")
    d = run([str(cli), "doctor"], env, check_rc=False)
    flags = {c["flag"]: c["ok"] for c in
             __import__("json").JSONDecoder().raw_decode(d.stdout)[0]
             .get("checks", [])}
    check(flags.get("model_embed") is True, "model flag cleared", flags)

    # ---- break 2: corrupt index ----------------------------------------
    idx_root = work / ".conf_knowledge_index"
    idx_root.mkdir(exist_ok=True)
    (idx_root / "bad.db").write_bytes(b"garbage" * 2000)
    d = run([str(cli), "doctor"], env, check_rc=False)
    check("corrupt" in d.stdout, "corrupt index flagged", d.stdout)
    sys.path.insert(0, str(REPO_SVC / "src"))
    from confluence_knowledge.index import Index
    from confluence_knowledge.settings import SetupError
    try:
        Index(idx_root / "bad.db")
        check(False, "corrupt index raises SetupError", "no error raised")
    except SetupError as e:
        check("unreadable" in str(e), "corrupt index raises SetupError", e)
    (idx_root / "bad.db").unlink()

    # ---- break 3: 401 → the error names `setup` -------------------------
    profile = {"env": "datacenter", "base_url": base, "email": None,
               "credential_ref": "e2e"}
    (home / "profile.json").write_text(json.dumps(profile))
    from confluence_knowledge.client import make_client
    from confluence_knowledge.settings import SiteProfile

    c = make_client(SiteProfile("datacenter", base, None, "e2e"), "bad-token")

    class R:
        status_code = 401
    check("setup" in c._describe(R()), "401 error names `setup`",
          c._describe(R()))

    # ---- break 4: registration removed ----------------------------------
    ws = sandbox / "repo"
    (ws / ".vscode").mkdir(parents=True)
    run([str(cli), "register", "--ide", "all", "--workspace", str(ws)], env,
        "register")
    run([str(cli), "register", "--remove", "--workspace", str(ws)], env,
        "unregister")
    d = run([str(cli), "doctor"], env, check_rc=False)
    check("registration" in d.stdout,
          "removed registration flagged", d.stdout)
    run([str(cli), "register", "--ide", "all", "--workspace", str(ws)], env,
        "re-register")
    d = run([str(cli), "doctor"], env, check_rc=False)
    flags = {c["flag"]: c["ok"] for c in
             __import__("json").JSONDecoder().raw_decode(d.stdout)[0]
             .get("checks", [])}
    check(flags.get("registration"), "registration flag cleared", flags)


    # ---- break N: bad upgrade — install ok but selftest fails → rollback --
    wheels = sandbox / "wheels"
    wheels.mkdir()
    outer = sys.executable
    # wheel-building helper — setuptools can't build under some hosted
    # toolchains (distutils shadowing), so each copied package gets
    # hatchling as a throwaway backend; the artifact is identical.
    import re as _re

    def _src_copy(name: str, version: str) -> Path:
        src2 = sandbox / f"pkg-{name}-{version}"
        shutil.copytree(REPO_SVC, src2,
                        ignore=shutil.ignore_patterns(
                            "dist", "build", "__pycache__", "*.egg-info",
                            ".venv", "tests", "scripts"))
        pp = src2 / "pyproject.toml"
        t = pp.read_text()
        t = _re.sub(r'version = "[^"]+"', f'version = "{version}"',
                    t, count=1)
        t = _re.sub(r'requires = \[[^\]]*\]',
                    'requires = ["hatchling"]', t, count=1)
        t = _re.sub(r'build-backend = "[^"]+"',
                    'build-backend = "hatchling.build"', t)
        pp.write_text(t)
        return src2

    # a healthy wheel of the CURRENT version — the rollback restore needs
    # somewhere to find <dist>==<installed> (not on any index yet)
    cur_src = _src_copy("cur", "0.0.0")  # version patched below
    pp = cur_src / "pyproject.toml"
    t = pp.read_text()
    inst_v = run([str(vpy), "-m", "pip", "show", "confluence-knowledge"], env,
                 "installed version").stdout.split("Version: ")[1].split()[0]
    t = _re.sub(r'version = "[^"]+"', f'version = "{inst_v}"',
                t, count=1)
    pp.write_text(t)
    run([outer, "-m", "pip", "wheel", str(cur_src), "-w", str(wheels),
         "--no-deps", "--quiet"], env, "build current-version wheel")
    bad_src = _src_copy("bad", "99.0.0")
    broken = bad_src / "src" / "confluence_knowledge" / "server.py"
    broken.write_text('raise RuntimeError("selftest-canary")\n'
                      + broken.read_text())
    run([outer, "-m", "pip", "wheel", str(bad_src), "-w", str(wheels),
         "--no-deps", "--quiet"], env, "build broken 99.0.0 wheel")
    env_upd = {**env, "PIP_FIND_LINKS": str(wheels),
               "KNOWLEDGE_UPDATE_INDEX": "http://127.0.0.1:9/{dist}"}
    u = run([str(vpy), "-m", "confluence_knowledge.cli", "update", "--to", "99.0.0"],
            env_upd, check_rc=False)
    check(u.returncode == 3, "broken update exits 3", u.returncode)
    check("rolled back" in u.stdout, "rollback reported",
          u.stdout[-1500:])
    v = run([str(vpy), "-m", "pip", "show", "confluence-knowledge"], env, "pip show")
    check("99.0.0" not in v.stdout.split("Version")[1],
          "previous version restored", v.stdout[:200])
    d = run([str(vpy), "-m", "confluence_knowledge.cli", "selftest"], env_upd,
            check_rc=False)
    check(d.returncode == 0, "restored install passes selftest",
          d.stdout[-800:])

    # healthy upgrade: same version bump, no injected failure — must stick
    good_src = _src_copy("good", "99.0.1")
    run([outer, "-m", "pip", "wheel", str(good_src), "-w", str(wheels),
         "--no-deps", "--quiet"], env, "build healthy 99.0.1 wheel")
    u = run([str(vpy), "-m", "confluence_knowledge.cli", "update", "--to", "99.0.1"],
            env_upd, check_rc=False)
    check(u.returncode == 0, "healthy update succeeds", u.stdout[-1500:])
    v = run([str(vpy), "-m", "pip", "show", "confluence-knowledge"], env, "pip show")
    check("99.0.1" in v.stdout.split("Version")[1],
          "healthy upgrade installed", v.stdout[:200])

    print("\nE2E FAILURE-MODE OK")
    srv.shutdown()


if __name__ == "__main__":
    main()
