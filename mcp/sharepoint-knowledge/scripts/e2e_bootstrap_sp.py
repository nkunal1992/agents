"""Bootstrap E2E for sharepoint-knowledge — fresh sandboxed HOME, real venv,
real install from the bundle, real stdio MCP, against a fake Graph:

    python discovery → venv → pip install → prepare-models →
    profile written (env token stands in for the device flow) → register →
    connect → crawl site → search → cited answer

Run from the repo:
    python scripts/e2e_bootstrap_sp.py [--service <path>] [--wheel]

Set CK_MODEL_CACHE=/path/to/models to pre-seed the models.
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
from _fake_graph_server import TOKEN, serve

REPO_SVC = Path(__file__).resolve().parents[1]
BUNDLE_SVC = REPO_SVC.parents[1] / "dist" / "sharepoint-knowledge-bundle" / "service"


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


def bootstrap(sandbox: Path, service_src: Path, use_wheel: bool,
              graph_base: str, spec: str | None = None,
              engine_spec: str | None = None):
    home = sandbox / "home"
    sp_home = home / ".sharepoint-knowledge"
    work = sandbox / "work"
    sp_home.mkdir(parents=True)
    work.mkdir()

    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "SP_KNOWLEDGE_HOME": str(sp_home),
           "SP_KNOWLEDGE_WORKDIR": str(work),
           "SP_KNOWLEDGE_GRAPH_BASE": graph_base,
           "SP_KNOWLEDGE_TOKEN": TOKEN,
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

    venv_dir = sp_home / "venv"
    fresh_venv(venv_dir)
    vpy = venv_bin(venv_dir) / ("python.exe" if os.name == "nt" else "python")
    cli = venv_bin(venv_dir) / ("sharepoint-knowledge.exe"
                              if os.name == "nt" else "sharepoint-knowledge")
    atexit.register(stop_shared_daemon, cli, env)

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
        wheels = sorted(service_src.parent.glob("sharepoint_knowledge-*.whl"))
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
              "'sharepoint-knowledge') or []))")],
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
    if cache and (Path(cache) / "nomic-embed-text-v1.5").exists():
        shutil.copytree(cache, models_dir, dirs_exist_ok=True)
        print(f"PASS models copied from cache {cache}")
    else:
        run([str(cli), "prepare-models"], env,
            "prepare-models (~1.3 GB real download)")

    # profile — what `setup` writes after the device sign-in; the env token
    # stands in for the Entra step (interactive by design, untestable here)
    from urllib.parse import urlparse
    site_url = f"http://127.0.0.1:{urlparse(graph_base).port}/sites/team"
    (sp_home / "profile.json").write_text(json.dumps({
        "tenant_id": "e2e-tenant", "client_id": "e2e-client",
        "site_url": site_url, "credential_ref": "e2e"}))

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
    check(jb and "sharepoint-knowledge" in jb[0].read_text(),
          "register: JetBrains user config written", written)
    merged = json.loads((ws / ".vscode" / "mcp.json").read_text())
    check("sharepoint-knowledge" in merged["servers"],
          "register: merged into .vscode/mcp.json")
    entry = merged["servers"]["sharepoint-knowledge"]
    if entry.get("type") == "http":
        check("/sharepoint-knowledge/mcp" in entry["url"]
              and entry.get("headers", {}).get("Authorization",
                                               "").startswith("Bearer "),
              "register: http entry points at the daemon's sp path",
              entry)
    else:
        check(entry["command"] == str(vpy)
              or (os.name == "nt"
                  and entry["command"] == str(vpy.with_name("pythonw.exe"))),
              "register: points at the venv python (pythonw on Windows)")

    doctor = run([str(cli), "doctor"], env, "doctor: models + auth path")
    check('"configured": true' in doctor.stdout,
          "doctor reports configured", doctor.stdout)
    return vpy, env, site_url


async def qa_roundtrip(vpy: Path, env: dict, site_url: str) -> None:
    params = StdioServerParameters(
        command=str(vpy), args=["-m", "sharepoint_knowledge.server_sp"],
        env=env, cwd=tempfile.gettempdir())
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        check({"status", "connect", "start_crawl", "crawl_step", "search",
               "get_evidence", "verify_citations"} <= tools,
              "fresh venv: all tools registered")
        c = await call(session, "connect", {"root": site_url})
        check(c["root"] == "site:site-guid-1", "connect: site resolution", c)
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
        check(r_["pages_seen"] == 3,
              f"pages + files indexed ({r_['pages_seen']})")
        check(any("secret" in w for w in r_.get("warnings", [])),
              "secret file skipped + disclosed")
        res = await call(session, "search",
                         {"scope_handle": c["scope_handle"],
                          "question": "When are production deploys allowed?"})
        check(res["coverage"] == "complete" and res["evidence"],
              "grounded answer with citations")
        assert "Thursdays" in res["evidence"][0]["text"], res["evidence"][0]


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
    srv, graph_base, hits, noauth = serve()
    sandbox = Path(tempfile.mkdtemp(prefix="sp-boot-")).resolve()
    print(f"sandbox: {sandbox}  fake-graph: {graph_base}")
    try:
        vpy, env, site_url = bootstrap(sandbox, service_src, use_wheel,
                                       graph_base, spec=spec,
                                       engine_spec=engine_spec)
        asyncio.run(qa_roundtrip(vpy, env, site_url))
        check(any(h == "/v1.0/me" for h in hits), "identity heartbeat hit")
        check(all(h.startswith("/download/") for h in noauth),
              "downloads carried no auth header", noauth[:3])
        check(all(h.startswith("/v1.0/") for h in hits),
              "no API calls outside Graph routes", hits[:5])

        # golden-set regression gate — evaluate the live post-crawl index
        # (shared evaluator lives in the engine package — R11 parity)
        idx_dbs = sorted((sandbox / "work" / ".sp_knowledge_index")
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
                      str(sandbox / "home" / ".sharepoint-knowledge")},
                     "golden-set eval")
            check("EVAL OK" in gv.stdout, "golden-set gate",
                  gv.stdout[-800:])
    finally:
        srv.shutdown()
    print("\nE2E BOOTSTRAP OK")


if __name__ == "__main__":
    main()
