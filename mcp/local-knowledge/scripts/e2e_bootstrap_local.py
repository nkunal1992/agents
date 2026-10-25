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
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                    / "confluence-knowledge" / "scripts"))
import atexit

from _e2e_common import call, check, fresh_venv, stop_shared_daemon


def _embed_dir_name() -> str:
    """Active embed model's on-disk dir (#249 — follows
    KNOWLEDGE_EMBED_MODEL / the spec registry)."""
    try:
        from confluence_knowledge.retrieval import active_embed_spec
        return active_embed_spec()["repo"].split("/")[-1]
    except Exception:
        return "snowflake-arctic-embed-xs"


REPO_SVC = Path(__file__).resolve().parents[1]
BUNDLE_SVC = REPO_SVC.parents[1] / "dist" / "local-knowledge-bundle" / "service"


def run(argv, env, ok_msg):
    p = subprocess.run(argv, check=False, env=env, capture_output=True,
                       text=True, encoding="utf-8",
                       errors="replace", timeout=600)
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


def bootstrap(sandbox: Path, service_src: Path, use_wheel: bool,
              spec: str | None = None, engine_spec: str | None = None):
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
    fresh_venv(venv_dir)
    vpy = venv_bin(venv_dir) / ("python.exe" if os.name == "nt" else "python")
    cli = venv_bin(venv_dir) / ("local-knowledge.exe" if os.name == "nt"
                              else "local-knowledge")
    atexit.register(stop_shared_daemon, cli, env)
    # engine first — confluence-knowledge>=0.4.0 may not be on the index yet
    # engine: --engine-spec <whl|dir> → install it + service --no-deps;
    # "index" → let pip resolve; default → repo source (not yet on index)
    no_deps = False
    if engine_spec == "index":
        pass
    else:
        eng = engine_spec or str(REPO_SVC.parents[0] / "confluence-knowledge")
        eng_install = f"{eng}[models,files]" if engine_spec else str(eng)
        run([str(vpy), "-m", "pip", "install", eng_install], env,
            f"engine install {Path(str(eng)).name}")
        no_deps = engine_spec is not None
    if spec:
        src_spec = spec
    elif use_wheel:
        wheels = sorted(service_src.parent.glob("local_knowledge-*.whl"))
        check(wheels, "bundle contains a wheel",
              list(service_src.parent.iterdir()))
        src_spec = f"{wheels[-1]}[models]"
    else:
        src_spec = f"{service_src}[models]"
    if no_deps:
        # engine pinned outside the index (e.g. an rc wheel that ~= specs
        # won't accept): install the service with --no-deps, then its
        # non-engine requirements from the installed metadata
        run([str(vpy), "-m", "pip", "install", "--no-deps", src_spec], env,
            f"venv + pip install --no-deps {src_spec}")
        import json as _json
        out = run(
            [str(vpy), "-c",
             ("import importlib.metadata, json; "
              "print(json.dumps(importlib.metadata.requires("
              "'local-knowledge') or []))")],
            env, "read service requires").stdout
        reqs = [r for r in _json.loads(out)
                if "confluence-knowledge" not in r and "extra ==" not in r]
        if reqs:
            run([str(vpy), "-m", "pip", "install"] + reqs, env,
                f"service deps {len(reqs)}")
    else:
        run([str(vpy), "-m", "pip", "install", src_spec], env,
            f"venv + pip install {src_spec}")

    cache = os.environ.get("CK_MODEL_CACHE")
    models_dir = Path(env["HOME"]) / ".knowledge" / "models"
    if cache and (Path(cache) / _embed_dir_name()).exists():
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
    # unified default is `knowledge` (0.10.1+); an older published
    # service still writes its per-service id — accept either shape
    kid = next((k for k in ("knowledge", "local-knowledge")
                if k in merged["servers"]), None)
    check("colleague-server" in merged["servers"] and kid,
          "register: merged existing .vscode/mcp.json without clobbering")
    entry = merged["servers"][kid]
    if entry.get("type") == "http":
        check(f"/{kid}/mcp" in entry["url"]
              and entry.get("headers", {}).get("Authorization",
                                               "").startswith("Bearer "),
              "register: http entry points at the daemon's unified path",
              entry)
    else:
        check(Path(entry["command"]).resolve().parent ==
              vpy.parent and
              Path(entry["command"]).stem in ("python", "pythonw"),
              "register: points at a venv interpreter")

    doctor = run([str(cli), "doctor"], env, "doctor: models ready")
    check('"flag": "model_embed"' in doctor.stdout and
          '"ok": true' in doctor.stdout,
          "doctor reports models via checks", doctor.stdout)

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
        check(st.get("checks") and all(c["ok"] for c in st["checks"]
              if c["flag"].startswith("model_")),
              "status: models ready via checks", st)
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
    spec = sys.argv[sys.argv.index("--spec") + 1] \
        if "--spec" in sys.argv else None
    engine_spec = (sys.argv[sys.argv.index("--engine-spec") + 1]
                   if "--engine-spec" in sys.argv else None)
    service_src = (BUNDLE_SVC if BUNDLE_SVC.exists() else REPO_SVC).resolve()
    if "--service" in sys.argv:
        service_src = Path(sys.argv[sys.argv.index("--service") + 1]).resolve()
    print(f"installing from: {'bundle wheel' if use_wheel else service_src}")
    sandbox = Path(tempfile.mkdtemp(prefix="lk-boot-")).resolve()
    docs = make_docs(sandbox)
    print(f"sandbox: {sandbox}")
    vpy, env = bootstrap(sandbox, service_src, use_wheel,
                         spec=spec, engine_spec=engine_spec)
    asyncio.run(qa_roundtrip(vpy, env, docs))

    # golden-set regression gate — evaluate the live post-crawl index
    # (shared evaluator lives in the engine package — R11 parity)
    idx_dbs = sorted((sandbox / "work" / ".local_knowledge_index")
                     .glob("*.db"))
    golden = Path(__file__).parent / "golden_set.sample.json"
    eval_script = (REPO_SVC.parents[0] / "confluence-knowledge"
                   / "scripts" / "eval_rag.py")
    if idx_dbs and golden.exists() and eval_script.exists():
        gv = run([str(vpy), str(eval_script),
                  "--golden", str(golden),
                  "--index", str(idx_dbs[0]),
                  "--report", str(sandbox / "eval_report.json")],
                 {**env, "CK_MODEL_HOME":
                  str(sandbox / "home" / ".local-knowledge")},
                 "golden-set eval")
        check("EVAL OK" in gv.stdout, "golden-set gate", gv.stdout[-800:])
    print("\nE2E BOOTSTRAP OK")


if __name__ == "__main__":
    main()
