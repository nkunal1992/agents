"""Failure-mode e2e for sharepoint-knowledge — model/index/registration
breaks + the creds flag (against the fake Graph).  Run:
    python scripts/e2e_failure_sp.py   (set CK_MODEL_CACHE to pre-seed)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                    / "confluence-knowledge" / "scripts"))
from _e2e_common import check
from _fake_graph_server import TOKEN, serve

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
    srv, graph_base, _hits, _noauth = serve()
    sandbox = Path(tempfile.mkdtemp(prefix="sp-fail-")).resolve()
    home = sandbox / "home" / ".sharepoint-knowledge"
    work = sandbox / "work"
    home.mkdir(parents=True)
    work.mkdir()
    env = {**os.environ, "HOME": str(sandbox / "home"),
           "USERPROFILE": str(sandbox / "home"),
           "SP_KNOWLEDGE_HOME": str(home),
           "SP_KNOWLEDGE_WORKDIR": str(work),
           "SP_KNOWLEDGE_GRAPH_BASE": graph_base,
           "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    site_url = f"http://127.0.0.1:{urlparse(graph_base).port}/sites/team"
    print(f"sandbox: {sandbox}  fake-graph: {graph_base}")

    venv_dir = home / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    bindir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    vpy = bindir / ("python.exe" if os.name == "nt" else "python")
    cli = bindir / ("sharepoint-knowledge.exe" if os.name == "nt"
                    else "sharepoint-knowledge")
    engine_spec = (sys.argv[sys.argv.index("--engine-spec") + 1]
                   if "--engine-spec" in sys.argv else None)
    spec = sys.argv[sys.argv.index("--spec") + 1] \
        if "--spec" in sys.argv else str(REPO_SVC) + "[models]"
    if engine_spec == "index":
        pass
    else:
        eng = engine_spec or str(REPO_SVC.parent / "confluence-knowledge")
        eng_install = f"{eng}[models,files]" if engine_spec else str(eng)
        run([str(vpy), "-m", "pip", "install", eng_install], env,
            "engine install")
    if engine_spec and engine_spec != "index":
        run([str(vpy), "-m", "pip", "install", "--no-deps", spec], env,
            "service install (no-deps)")
        import importlib.metadata as _md
        reqs = [r for r in (_md.requires("sharepoint-knowledge") or [])
                if "confluence-knowledge" not in r and "extra ==" not in r]
        if reqs:
            run([str(vpy), "-m", "pip", "install"] + reqs, env,
                "service deps")
    else:
        run([str(vpy), "-m", "pip", "install", spec], env,
            "install")

    cache = os.environ.get("CK_MODEL_CACHE")
    if cache and (Path(cache) / "bge-m3").exists():
        import shutil
        shutil.copytree(cache, home / "models", dirs_exist_ok=True)
        print("PASS models seeded from cache")
    else:
        run([str(cli), "prepare-models"], env, "prepare-models")

    # break 0a: NO sign-in → doctor creds flag red
    d = run([str(cli), "doctor"], env, check_rc=False)
    check("creds_valid" in d.stdout or "sign-in" in d.stdout,
          "missing sign-in flagged", d.stdout)

    # sign in (env token stands in for the device flow)
    env["SP_KNOWLEDGE_TOKEN"] = TOKEN
    (home / "profile.json").write_text(json.dumps({
        "tenant_id": "e2e", "client_id": "e2e", "site_url": site_url,
        "credential_ref": "e2e"}))
    d = run([str(cli), "doctor"], env, check_rc=False)
    flags = {c["flag"]: c["ok"] for c in
             __import__("json").JSONDecoder().raw_decode(d.stdout)[0]
             .get("checks", [])}
    check(flags.get("creds_valid"), "valid token → creds flag green", flags)

    # break 1: partial model
    (home / "models" / "bge-m3" / "tokenizer_config.json").unlink()
    d = run([str(cli), "doctor"], env, check_rc=False)
    check("model_embed" in d.stdout and d.returncode != 0,
          "partial model flagged + non-zero", d.stdout)
    run([str(cli), "prepare-models"], env, "remediation")

    # break 2: corrupt index
    idx_root = work / ".sp_knowledge_index"
    idx_root.mkdir(exist_ok=True)
    (idx_root / "bad.db").write_bytes(b"garbage" * 2000)
    d = run([str(cli), "doctor"], env, check_rc=False)
    check("corrupt" in d.stdout, "corrupt index flagged", d.stdout)
    (idx_root / "bad.db").unlink()

    # break 3: revoked token — fake Graph 401s → creds flag red again
    env["SP_KNOWLEDGE_TOKEN"] = "wrong-token"
    d = run([str(cli), "doctor"], env, check_rc=False)
    check("creds_valid" in d.stdout and "setup" in d.stdout,
          "revoked token → creds flag + setup remediation", d.stdout)


    # ---- break N: bad upgrade — install ok but selftest fails → rollback --
    wheels = sandbox / "wheels"
    wheels.mkdir()
    outer = sys.executable
    # a healthy wheel of the CURRENT version — the rollback restore needs
    # somewhere to find <dist>==<installed> (not on any index yet)
    run([outer, "-m", "pip", "wheel", str(REPO_SVC), "-w", str(wheels),
         "--no-deps", "--quiet"], env, "build current-version wheel")
    bad_src = sandbox / "bad-pkg"
    import re as _re
    shutil.copytree(REPO_SVC, bad_src,
                    ignore=shutil.ignore_patterns(
                        "dist", "build", "__pycache__", "*.egg-info",
                        ".venv", "tests", "scripts"))
    pp = bad_src / "pyproject.toml"
    pp.write_text(_re.sub(r'version = "[^"]+"', 'version = "99.0.0"',
                          pp.read_text(), count=1))
    broken = bad_src / "src" / "sharepoint_knowledge" / "server_sp.py"
    broken.write_text('raise RuntimeError("selftest-canary")\n'
                      + broken.read_text())
    run([outer, "-m", "pip", "wheel", str(bad_src), "-w", str(wheels),
         "--no-deps", "--quiet"], env, "build broken 99.0.0 wheel")
    env_upd = {**env, "PIP_FIND_LINKS": str(wheels),
               "KNOWLEDGE_UPDATE_INDEX": "http://127.0.0.1:9/{dist}"}
    u = run([str(cli), "update", "--to", "99.0.0"], env_upd,
            check_rc=False)
    check(u.returncode == 3, "broken update exits 3", u.returncode)
    check("rolled back" in u.stdout, "rollback reported",
          u.stdout[-1500:])
    v = run([str(vpy), "-m", "pip", "show", "sharepoint-knowledge"], env, "pip show")
    check("99.0.0" not in v.stdout.split("Version")[1],
          "previous version restored", v.stdout[:200])
    d = run([str(cli), "selftest"], env_upd, check_rc=False)
    check(d.returncode == 0, "restored install passes selftest",
          d.stdout[-800:])

    # healthy upgrade: same version bump, no injected failure — must stick
    good_src = sandbox / "good-pkg"
    shutil.copytree(REPO_SVC, good_src,
                    ignore=shutil.ignore_patterns(
                        "dist", "build", "__pycache__", "*.egg-info",
                        ".venv", "tests", "scripts"))
    pp = good_src / "pyproject.toml"
    pp.write_text(_re.sub(r'version = "[^"]+"', 'version = "99.0.1"',
                          pp.read_text(), count=1))
    run([outer, "-m", "pip", "wheel", str(good_src), "-w", str(wheels),
         "--no-deps", "--quiet"], env, "build healthy 99.0.1 wheel")
    u = run([str(cli), "update", "--to", "99.0.1"], env_upd,
            check_rc=False)
    check(u.returncode == 0, "healthy update succeeds", u.stdout[-1500:])
    v = run([str(vpy), "-m", "pip", "show", "sharepoint-knowledge"], env, "pip show")
    check("99.0.1" in v.stdout.split("Version")[1],
          "healthy upgrade installed", v.stdout[:200])

    print("\nE2E FAILURE-MODE OK")
    srv.shutdown()


if __name__ == "__main__":
    main()
