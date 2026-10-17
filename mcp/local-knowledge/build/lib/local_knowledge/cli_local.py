"""local-knowledge CLI: doctor / prepare-models / register / serve / clean.

No `setup` — there are no credentials to collect. connect() takes the path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from confluence_knowledge.retrieval import prepare_models
from confluence_knowledge.settings import SetupError

from .settings_local import (
    APP_NAME,
    INDEX_DIR_NAME,
    home,
    workdir,
)

WORKDIR_ENV = "LOCAL_KNOWLEDGE_WORKDIR"


def _doctor() -> int:
    from confluence_knowledge.update import check

    from .server_local import status
    s = status()
    print(json.dumps(s, indent=2))
    bad = [c for c in s.get("checks", []) if not c["ok"]]
    if bad:
        print("\nhealth flags:")
        for c in bad:
            print(f"  ✗ {c['flag']}: {c['detail']}")
            if c.get("remediation"):
                print(f"    → {c['remediation']}")
    upd = check("local-knowledge")
    if upd["installed"]:
        line = f"version: {upd['installed']}"
        if upd["latest"]:
            line += f" (latest: {upd['latest']})"
        elif upd["latest"] is None:
            line += " (latest: unchecked — offline or internal mirror)"
        print(line)
    if upd["update_available"]:
        print("update available: run `local-knowledge update` "
              "(or pip install -U local-knowledge[models])")
    return 4 if bad else 0


def main() -> None:
    ap = argparse.ArgumentParser(prog=APP_NAME)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="verify install + models")
    sub.add_parser("prepare-models", help="download the pinned local models")
    sub.add_parser("update", help="upgrade this package via pip -U")
    reg = sub.add_parser(
        "register",
        help="write this server into the IDE's MCP config (no manual paste)")
    reg.add_argument("--ide", default="auto",
                     choices=["auto", "all", "vscode", "jetbrains"])
    reg.add_argument("--workspace", type=Path, default=None)
    reg.add_argument("--remove", action="store_true")
    sub.add_parser("serve", help="run the stdio MCP server")
    clean = sub.add_parser("clean", help="delete the local index")
    clean.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if args.cmd == "doctor":
        sys.exit(_doctor())
    if args.cmd == "prepare-models":
        for line in prepare_models(home()):
            print(line)
        return
    if args.cmd == "update":
        import subprocess
        cmd = [sys.executable, "-m", "pip", "install", "-U",
               "local-knowledge[models]"]
        if os.name == "nt":
            # this exe is the running process on Windows — pip can't replace
            # a file that's in use; hand the user the equivalent command
            print("on Windows, upgrade via the venv python directly:\n  "
                  + " ".join(f'"{c}"' if " " in c else c for c in cmd))
            return
        r = subprocess.run(cmd, check=False)
        if r.returncode == 0:
            print("\nupdated — run `local-knowledge prepare-models` "
                  "(new releases may pin new model artifacts) then "
                  "`local-knowledge doctor`")
        sys.exit(r.returncode)
    if args.cmd == "register":
        from confluence_knowledge.registration import register
        try:
            wd = (Path(os.environ[WORKDIR_ENV]).expanduser().resolve()
                  if os.environ.get(WORKDIR_ENV) else None)
            results = register(ide=args.ide, workspace=args.workspace,
                               workdir=wd, remove=args.remove,
                               server_id=APP_NAME,
                               module="local_knowledge.server_local",
                               env_var=WORKDIR_ENV)
        except SetupError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(2)
        for path, action in results:
            print(f"{action}: {path}")
        if not args.remove:
            print("\nReload your IDE so Copilot picks up the server, "
                  "then re-ask the agent.")
        return
    if args.cmd == "serve":
        from .server_local import main as serve
        serve()
        return
    if args.cmd == "clean":
        import shutil

        target = workdir() / INDEX_DIR_NAME
        if target.exists() and (args.yes or input(
                f"delete {target}? [y/N] ").strip().lower() == "y"):
            shutil.rmtree(target)
            print("index deleted")
        return


if __name__ == "__main__":
    main()
