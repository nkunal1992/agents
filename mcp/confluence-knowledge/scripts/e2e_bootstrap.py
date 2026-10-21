"""Bootstrap E2E — replay the agent's install steps exactly, on a fresh HOME.

Simulates a colleague's first run with no IDE: the same commands the agent
would execute, in the same order, against a fake Confluence:

    python discovery → venv → pip install <service>[models] → prepare-models
    → (credentials written as the wizard would leave them) → register
    → launch the venv's real server over stdio → full Q&A tool chain

Everything runs in a sandboxed $HOME so the test never touches the real
~/.confluence-knowledge. Run from the repo checkout:

    python scripts/e2e_bootstrap.py [--service <path/to/service>]

--service defaults to the dist/ bundle copy when present (test what ships),
else the repo source tree. First run downloads the models for real; set
CK_MODEL_CACHE=/path/to/models to pre-seed them (~1.3 GB).
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
from _e2e_common import TOKEN, call, check, serve

REPO_SVC = Path(__file__).resolve().parents[1]
BUNDLE_SVC = REPO_SVC.parents[1] / "dist" / "confluence-knowledge-bundle" / "service"


def run(argv, env, ok_msg, check_rc=True):
    p = subprocess.run(argv, check=False, env=env, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600)
    if check_rc and p.returncode != 0:
        print(f"FAIL: {' '.join(map(str, argv))}\n{p.stdout}\n{p.stderr}",
              flush=True)
        os._exit(1)
    print("PASS", ok_msg)
    return p


def venv_bin(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def bootstrap(sandbox: Path, service_src: Path, use_wheel: bool = False,
              spec: str | None = None):
    home = sandbox / "home"
    ck_home = home / ".confluence-knowledge"
    work = sandbox / "work"
    ck_home.mkdir(parents=True)
    work.mkdir(parents=True)

    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
           "CONF_KNOWLEDGE_HOME": str(ck_home), "CONF_KNOWLEDGE_WORKDIR": str(work),
           "CONF_KNOWLEDGE_TOKEN": TOKEN, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}

    # 1. python discovery — same order the agent uses
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

    # 2. venv + install from the service source — what the agent runs
    venv_dir = ck_home / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    vpy = venv_bin(venv_dir) / ("python.exe" if os.name == "nt" else "python")
    cli = venv_bin(venv_dir) / ("confluence-knowledge.exe" if os.name == "nt"
                              else "confluence-knowledge")
    if spec:
        src_spec = spec
    elif use_wheel:
        wheels = sorted(service_src.parent.glob("confluence_knowledge-*.whl"))
        check(wheels, "bundle contains a wheel", list(service_src.parent.iterdir()))
        src_spec = f"{wheels[-1]}[models]"
    else:
        src_spec = f"{service_src}[models]"
    run([str(vpy), "-m", "pip", "install", src_spec], env,
        f"venv + pip install {src_spec}")

    # 3. models — reuse a cache dir if provided, else download for real
    cache = os.environ.get("CK_MODEL_CACHE")
    cache = os.path.expanduser(cache) if cache else None
    models_dir = ck_home / "models"
    if cache and (Path(cache) / "bge-m3").exists():
        shutil.copytree(cache, models_dir, dirs_exist_ok=True)
        print(f"PASS models copied from cache {cache}")
    else:
        run([str(cli), "prepare-models"], env, "prepare-models (~1.3 GB real download)")

    # 4. credentials — the wizard writes profile.json + keyring; the keyring
    #    part is interactive, so we emulate the env-var credential path the
    #    client already supports and let doctor heartbeat it.
    srv, base, hits = serve()
    (ck_home / "profile.json").write_text(json.dumps(
        {"env": "datacenter", "base_url": base, "email": None,
         "credential_ref": "e2e"}))
    # pre-register doctor: registration flag is correctly red (exit 4)
    doctor = run([str(cli), "doctor"], env,
                 "doctor: configured + models + heartbeat", check_rc=False)
    check('"configured": true' in doctor.stdout, "doctor reports configured")
    check('"flag": "registration"' in doctor.stdout,
          "pre-register doctor flags missing registration", doctor.stdout)

    # 5. register — the new step: agent writes IDE config itself
    ws = sandbox / "repo"
    (ws / ".vscode").mkdir(parents=True)
    (ws / ".vscode" / "mcp.json").write_text(
        json.dumps({"servers": {"colleague-server": {"command": "x"}}}))
    reg_out = run([str(cli), "register", "--ide", "all", "--workspace", str(ws)],
                  env, "register: wrote IDE configs")
    # the JetBrains user-level file is OS-specific; find it in the command's
    # own output rather than hardcoding a path
    written = [ln.split(":", 1)[1].strip() for ln in reg_out.stdout.splitlines()
               if ":" in ln and ln.split(":", 1)[0] in ("written", "removed",
                                                        "backup-replaced")]
    jb = [Path(p) for p in written if "github-copilot" in p]
    check(jb and "confluence-knowledge" in jb[0].read_text(),
          "register: JetBrains user config written", written)
    merged = json.loads((ws / ".vscode" / "mcp.json").read_text())
    check("colleague-server" in merged["servers"]
          and "confluence-knowledge" in merged["servers"],
          "register: merged existing .vscode/mcp.json without clobbering")
    entry = merged["servers"]["confluence-knowledge"]
    check(entry["command"] == str(vpy), "register: points at the venv python")

    # post-register doctor: all flags green → exit 0
    doctor = run([str(cli), "doctor"], env,
                 "doctor green after register", check_rc=False)
    check(doctor.returncode == 0 and '"ok": false' not in doctor.stdout,
          "all health flags green after register", doctor.returncode)

    return vpy, env, srv, base, hits


async def qa_roundtrip(vpy: Path, env: dict) -> None:
    """Full tool chain over real stdio from the freshly installed venv."""
    params = StdioServerParameters(command=str(vpy),
                                   args=["-m", "confluence_knowledge.server"],
                                   env=env, cwd=tempfile.gettempdir())
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}
        check({"status", "connect", "start_crawl", "crawl_step", "search",
               "get_evidence", "verify_citations", "submit_feedback",
               "feedback_report"} <= tools,
              "fresh venv: all tools registered")
        st = await call(session, "status")
        check(st["configured"], "status: configured")
        c = await call(session, "connect", {"root": "100"})
        job = await call(session, "start_crawl", {"scope_handle": c["scope_handle"]})
        steps, r_, waited = 0, {"state": "running"}, 0.0
        while r_["state"] != "done":
            steps += 1
            check(waited <= 90, "background crawl finishes", r_)
            await asyncio.sleep(0.4)
            waited += 0.4
            r_ = await call(session, "crawl_step",
                            {"scope_handle": c["scope_handle"], "job_id": job["job_id"]})
        check(r_["pages_seen"] == 32, f"crawled 32 pages ({steps} polls)")
        res = await call(session, "search", {"scope_handle": c["scope_handle"],
                                             "question": "When can we deploy?"})
        check(res["coverage"] == "complete"
              and res["evidence"][0]["page_title"] == "Deploy Windows",
              "fresh install: grounded answer with citations")

        # feedback loop: correction stored → same-topic search surfaces it
        fb = await call(session, "submit_feedback",
                        {"scope_handle": c["scope_handle"],
                         "question": "when can we deploy",
                         "correction": "freeze now starts December 15",
                         "note": "deploy calendar changed"})
        check(fb["stored"], "submit_feedback stores a correction")
        res2 = await call(session, "search",
                          {"scope_handle": c["scope_handle"],
                           "question": "when are production deploys allowed"})
        check(res2["user_corrections"]
              and "December 15" in res2["user_corrections"][0]["correction"],
              "search auto-discloses matching correction")
        rep = await call(session, "feedback_report",
                         {"scope_handle": c["scope_handle"]})
        check(rep["count"] == 1, "feedback_report aggregates")


def main() -> None:
    use_wheel = "--wheel" in sys.argv
    spec = sys.argv[sys.argv.index("--spec") + 1] \
        if "--spec" in sys.argv else None
    service_src = (BUNDLE_SVC if BUNDLE_SVC.exists() else REPO_SVC).resolve()
    if "--service" in sys.argv:
        service_src = Path(sys.argv[sys.argv.index("--service") + 1]).resolve()
    print(f"installing from: {'bundle wheel' if use_wheel else service_src}")
    sandbox = Path(tempfile.mkdtemp(prefix="ck-boot-")).resolve()
    print(f"sandbox: {sandbox}")
    vpy, env, srv, _base, hits = bootstrap(sandbox, service_src, use_wheel,
                                       spec=spec)
    asyncio.run(qa_roundtrip(vpy, env))
    check(all(h.startswith("/") for h in hits), "no outbound beyond localhost",
          hits[:5])
    srv.shutdown()

    # golden-set regression gate — evaluate the live post-crawl index
    work = sandbox / "work"
    idx_dbs = sorted((work / ".conf_knowledge_index").glob("*.db"))
    if idx_dbs and (Path(__file__).parent / "golden_set.sample.json").exists():
        gv = run([str(vpy), str(Path(__file__).parent / "eval_rag.py"),
                  "--golden", str(Path(__file__).parent
                                  / "golden_set.sample.json"),
                  "--index", str(idx_dbs[0]),
                  "--report", str(sandbox / "eval_report.json")],
                 env, "golden-set eval")
        check("EVAL OK" in gv.stdout, "golden-set gate", gv.stdout[-800:])

    print("\nE2E BOOTSTRAP OK")


if __name__ == "__main__":
    main()
