"""Failure-mode e2e for local-knowledge — model/index/registration breaks.
Run: python scripts/e2e_failure_local.py   (set CK_MODEL_CACHE to pre-seed)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                    / "confluence-knowledge" / "scripts"))
from _e2e_common import check

REPO_SVC = Path(__file__).resolve().parents[1]


def run(argv, env, ok_msg=None, check_rc=True):
    p = subprocess.run(argv, check=False, env=env, capture_output=True,
                       text=True, timeout=600)
    if check_rc and p.returncode != 0:
        print(f"FAIL: {' '.join(map(str, argv))}\n{p.stdout}\n{p.stderr}",
              flush=True)
        os._exit(1)
    if ok_msg:
        print("PASS", ok_msg)
    return p


def main() -> None:
    sandbox = Path(tempfile.mkdtemp(prefix="lk-fail-")).resolve()
    home = sandbox / "home" / ".local-knowledge"
    work = sandbox / "work"
    home.mkdir(parents=True)
    work.mkdir()
    env = {**os.environ, "HOME": str(sandbox / "home"),
           "USERPROFILE": str(sandbox / "home"),
           "LOCAL_KNOWLEDGE_HOME": str(home),
           "LOCAL_KNOWLEDGE_WORKDIR": str(work),
           "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    print(f"sandbox: {sandbox}")

    venv_dir = home / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    bindir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    vpy = bindir / ("python.exe" if os.name == "nt" else "python")
    cli = bindir / ("local-knowledge.exe" if os.name == "nt"
                    else "local-knowledge")
    engine = REPO_SVC.parent / "confluence-knowledge"
    run([str(vpy), "-m", "pip", "install", str(engine)], env, "engine install")
    run([str(vpy), "-m", "pip", "install", str(REPO_SVC) + "[models]"], env,
        "install")

    cache = os.environ.get("CK_MODEL_CACHE")
    if cache and (Path(cache) / "bge-m3").exists():
        import shutil
        shutil.copytree(cache, home / "models", dirs_exist_ok=True)
        print("PASS models seeded from cache")
    else:
        run([str(cli), "prepare-models"], env, "prepare-models")

    # break 1: partial model
    (home / "models" / "bge-m3" / "onnx" /
     "sentence_transformers_quantized.onnx").unlink()
    d = run([str(cli), "doctor"], env, check_rc=False)
    check("model_embed" in d.stdout and d.returncode != 0,
          "partial model flagged + non-zero", d.stdout)
    run([str(cli), "prepare-models"], env, "remediation")

    # break 2: corrupt index
    idx_root = work / ".local_knowledge_index"
    idx_root.mkdir(exist_ok=True)
    (idx_root / "bad.db").write_bytes(b"garbage" * 2000)
    d = run([str(cli), "doctor"], env, check_rc=False)
    check("corrupt" in d.stdout, "corrupt index flagged", d.stdout)
    (idx_root / "bad.db").unlink()

    # break 3: registration removed
    ws = sandbox / "repo"
    (ws / ".vscode").mkdir(parents=True)
    run([str(cli), "register", "--ide", "all", "--workspace", str(ws)], env)
    run([str(cli), "register", "--remove", "--workspace", str(ws)], env)
    d = run([str(cli), "doctor"], env, check_rc=False)
    check("registration" in d.stdout, "removed registration flagged",
          d.stdout)

    print("\nE2E FAILURE-MODE OK")


if __name__ == "__main__":
    main()
