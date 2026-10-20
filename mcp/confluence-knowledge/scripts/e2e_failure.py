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
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _e2e_common import check, serve

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
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    bindir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    vpy = bindir / ("python.exe" if os.name == "nt" else "python")
    cli = bindir / ("confluence-knowledge.exe" if os.name == "nt"
                    else "confluence-knowledge")
    spec = sys.argv[sys.argv.index("--spec") + 1] \
        if "--spec" in sys.argv else str(REPO_SVC) + "[models]"
    run([str(vpy), "-m", "pip", "install", spec], env,
        "install")

    cache = os.environ.get("CK_MODEL_CACHE")
    if cache and (Path(cache) / "bge-m3").exists():
        import shutil
        shutil.copytree(cache, home / "models", dirs_exist_ok=True)
        print("PASS models seeded from cache")
    else:
        run([str(cli), "prepare-models"], env, "prepare-models")

    # ---- break 1: partial model dir ------------------------------------
    onnx = (home / "models" / "bge-m3" / "tokenizer_config.json")
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

    print("\nE2E FAILURE-MODE OK")
    srv.shutdown()


if __name__ == "__main__":
    main()
