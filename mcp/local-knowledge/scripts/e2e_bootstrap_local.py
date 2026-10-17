"""Bootstrap E2E for local-knowledge — replay the agent's install steps on a
fresh sandboxed HOME, against a generated document tree:

    python discovery → venv → pip install <service>[models] → prepare-models
    → register → launch the venv's real server over stdio →
    connect(folder) → crawl → search → cited answer

No fake server needed — there is no HTTP path at all. Run from the repo:

    python scripts/e2e_bootstrap_local.py [--service <path>] [--wheel]

--service defaults to the dist/ bundle copy when present. Set
CK_MODEL_CACHE=/path/to/models to pre-seed the models (~1.3 GB download
otherwise).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                    / "confluence-knowledge" / "scripts"))
from _e2e_common import call, check

REPO_SVC = Path(__file__).resolve().parents[1]
BUNDLE_SVC = REPO_SVC.parents[1] / "dist" / "local-knowledge-bundle" / "service"


def run(argv, env, ok_msg):
    p = subprocess.run(argv, check=False, env=env, capture_output=True,
                       text=True, timeout=600)
    if p.returncode != 0:
        print(f"FAIL: {' '.join(map(str, argv))}\n{p.stdout}\n{p.stderr}",
              flush=True)
        os._exit(1)
    print("PASS", ok_msg)
    return p


def venv_bin(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def make_docs(sandbox: Path) -> Path:
    """A doc tree a colleague would actually index — plus traps."""
    docs = sandbox / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "deploy.md").write_text(
        "# Runbook\n\nProduction deploys happen only on Thursdays after 18:00.\n"
        "The gateway must be restarted before every deploy.\n")
    (docs / "sub" / "owners.csv").write_text("service,owner\ngateway,ops-team\n")
    (docs / ".env").write_text("API_KEY=should-never-be-indexed")
    (docs / "id_rsa").write_text("PRIVATE KEY MATERIAL")
    return docs


def bootstrap(sandbox: Path, service_src: Path, use_wheel: bool):
    home = sandbox / "home"
    lk_home = home / ".local-knowledge"
    work = sandbox / "work"
    lk_home.mkdir(parents=True)
    work.mkdir()

    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "LOCAL_KNOWLEDGE_HOME": str(lk_home),
           "LOCAL_KNOWLEDGE_WORKDIR": str(work),
           "PIP_DISABLE_PIP_VERSION_CHECK": "1"}

    py = None
    for cand in ("python3.13", "python3.12", "python3.11", "python3",
                 "python", "py"):
        exe = shutil.which(cand)
        if exe:
            r = subprocess.run([exe, "-c",
                                "import sys; print(sys.version_info >= (3,11))"],
                               check=False, capture_output=True, text=True)
            if r.stdout.strip() == "True":
                py = exe
                break
    check(py, f"preflight: python >=3.11 found ({py or 'none'})")

    venv_dir = lk_home / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    vpy = venv_bin(venv_dir) / ("python.exe" if os.name == "nt" else "python")
    cli = venv_bin(venv_dir) / ("local-knowledge.exe" if os.name == "nt"
                              else "local-knowledge")
    # engine first — confluence-knowledge>=0.4.0 may not be on the index yet
    engine = REPO_SVC.parents[0] / "confluence-knowledge"
    run([str(vpy), "-m", "pip", "install", str(engine)], env,
        f"engine install {engine.name}")
    if use_wheel:
        wheels = sorted(service_src.parent.glob("local_knowledge-*.whl"))
        check(wheels, "bundle contains a wheel",
              list(service_src.parent.iterdir()))
        src_spec = f"{wheels[-1]}[models]"
    else:
        src_spec = f"{service_src}[models]"
    run([str(vpy), "-m", "pip", "install", src_spec], env,
        f"venv + pip install {src_spec}")

    cache = os.environ.get("CK_MODEL_CACHE")
    models_dir = lk_home / "models"
    if cache and (Path(cache) / "bge-m3").exists():
        shutil.copytree(cache, models_dir, dirs_exist_ok=True)
        print(f"PASS models copied from cache {cache}")
    else:
        run([str(cli), "prepare-models"], env,
            "prepare-models (~1.3 GB real download)")

    # register — no credentials step at all (there is no `setup`)
    ws = sandbox / "repo"
    (ws / ".vscode").mkdir(parents=True)
    (ws / ".vscode" / "mcp.json").write_text(
        json.dumps({"servers": {"colleague-server": {"command": "x"}}}))
    reg_out = run([str(cli), "register", "--ide", "all", "--workspace", str(ws)],
                  env, "register: wrote IDE configs")
    written = [ln.split(":", 1)[1].strip() for ln in reg_out.stdout.splitlines()
               if ":" in ln and ln.split(":", 1)[0] in ("written", "removed",
                                                        "backup-replaced")]
    jb = [Path(p) for p in written if "github-copilot" in p]
    check(jb and "local-knowledge" in jb[0].read_text(),
          "register: JetBrains user config written", written)
    merged = json.loads((ws / ".vscode" / "mcp.json").read_text())
    check("colleague-server" in merged["servers"]
          and "local-knowledge" in merged["servers"],
          "register: merged existing .vscode/mcp.json without clobbering")
    check(merged["servers"]["local-knowledge"]["command"] == str(vpy),
          "register: points at the venv python")

    doctor = run([str(cli), "doctor"], env, "doctor: models ready")
    check('"embedding_model_ready": true' in doctor.stdout,
          "doctor reports both models")

    return vpy, env


async def qa_roundtrip(vpy: Path, env: dict, docs: Path) -> None:
    params = StdioServerParameters(
        command=str(vpy), args=["-m", "local_knowledge.server_local"],
        env=env, cwd=tempfile.gettempdir())
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        check({"status", "connect", "start_crawl", "crawl_step", "search",
               "get_evidence", "verify_citations"} <= tools,
              "fresh venv: all tools registered")
        st = await call(session, "status")
        check("models" in str(st) or st.get("embedding_model_ready"),
              "status: models ready")
        c = await call(session, "connect", {"root": str(docs)})
        check(c["kind"] == "folder", "connect: folder scope")
        job = await call(session, "start_crawl",
                         {"scope_handle": c["scope_handle"]})
        r_, waited = {"state": "running"}, 0.0
        while r_["state"] != "done":
            check(waited <= 90, "background crawl finishes", r_)
            await asyncio.sleep(0.4)
            waited += 0.4
            r_ = await call(session, "crawl_step",
                            {"scope_handle": c["scope_handle"],
                             "job_id": job["job_id"]})
        check(r_["pages_seen"] >= 2, f"indexed the doc tree ({r_['pages_seen']} files)")
        warns = " ".join(r_.get("warnings", []))
        check("secret" in warns, "secret-looking files skipped + disclosed", warns)
        res = await call(session, "search",
                         {"scope_handle": c["scope_handle"],
                          "question": "When are production deploys allowed?"})
        check(res["coverage"] == "complete"
              and res["evidence"][0]["page_title"] == "deploy.md",
              "grounded answer with file citations", res.get("coverage"))
        check(res["evidence"][0]["page_url"].startswith("file://"),
              "citation URLs are file:// absolute paths")


def main() -> None:
    use_wheel = "--wheel" in sys.argv
    service_src = (BUNDLE_SVC if BUNDLE_SVC.exists() else REPO_SVC).resolve()
    if "--service" in sys.argv:
        service_src = Path(sys.argv[sys.argv.index("--service") + 1]).resolve()
    print(f"installing from: {'bundle wheel' if use_wheel else service_src}")
    sandbox = Path(tempfile.mkdtemp(prefix="lk-boot-")).resolve()
    docs = make_docs(sandbox)
    print(f"sandbox: {sandbox}")
    vpy, env = bootstrap(sandbox, service_src, use_wheel)
    asyncio.run(qa_roundtrip(vpy, env, docs))
    print("\nE2E BOOTSTRAP OK")


if __name__ == "__main__":
    main()
