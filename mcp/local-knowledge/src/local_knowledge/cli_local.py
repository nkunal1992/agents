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



MODULE = "local_knowledge.server_local"
ENV_VAR = WORKDIR_ENV
def _doctor() -> int:
    from confluence_knowledge.update import check

    from .server_local import status
    s = status()
    upd = check("local-knowledge")
    if upd.get("installed_yanked"):
        s.setdefault("checks", []).append({
            "flag": "release", "ok": False,
            "detail": f"installed {upd['installed']} was yanked"
                      + (f" ({upd['yank_reason']})"
                         if upd.get("yank_reason") else ""),
            "remediation": "local-knowledge update "
                           "(moves to the last-good release)"})
    print(json.dumps(s, indent=2))
    bad = [c for c in s.get("checks", []) if not c["ok"]]
    if bad:
        print("\nhealth flags:")
        for c in bad:
            print(f"  ✗ {c['flag']}: {c['detail']}")
            if c.get("remediation"):
                print(f"    → {c['remediation']}")
    if upd["installed"]:
        line = f"version: {upd['installed']}"
        if upd["latest"]:
            line += f" (latest: {upd['latest']})"
        elif upd["latest"] is None:
            line += " (latest: unchecked — offline or internal mirror)"
        if upd.get("pending"):
            line += (f" — {upd['pending']['version']} soaking until "
                     f"{upd['pending']['offered_after']}")
        print(line)

    # self-heal the daemon http endpoint (#206): a dead daemon or a stale
    # port in the registered URL is what leaves the IDE with dead tools —
    # restarting the daemon + rewriting the entry recovers it, no reload
    if any(c["flag"] == "daemon_http" and not c["ok"]
           for c in s.get("checks", [])):
        try:
            from confluence_knowledge.registration import register
            from confluence_knowledge.rpc import RpcDb, ensure_daemon
            from confluence_knowledge.settings import daemon_home
            root = daemon_home()
            ensure_daemon(RpcDb(root / "rpc.db"))
            results = register(transport="http", workdir=root,
                               server_id=APP_NAME, module=MODULE,
                               env_var=ENV_VAR)
            print(f"self-heal: daemon restarted, http entry refreshed "
                  f"in {len(results)} config(s)")
        except Exception as e:                  # noqa: BLE001
            print(f"self-heal failed: {e} — run "
                  "register --transport http", file=sys.stderr)
    if upd["update_available"]:
        print("update available: run `local-knowledge update` "
              "(soaked release, self-tested, rolls back on failure)")
    return 4 if bad else 0


def main() -> None:
    ap = argparse.ArgumentParser(prog=APP_NAME)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="verify install + models")
    sub.add_parser("prepare-models", help="download the pinned local models")
    u = sub.add_parser("update", help="safe upgrade — soaked release, "
                     "self-test, auto-rollback")
    g = u.add_mutually_exclusive_group()
    g.add_argument("--latest", action="store_true",
                   help="skip the release soak")
    g.add_argument("--to", metavar="VERSION",
                   help="install an exact version")
    g.add_argument("--rollback", action="store_true",
                   help="restore the previous environment")
    sub.add_parser("selftest", help="verify the install end to end "
                   "(tools, models, mini-RAG)")
    reg = sub.add_parser(
        "register",
        help="write this server into the IDE's MCP config (no manual paste)")
    reg.add_argument("--ide", default="auto",
                     choices=["auto", "all", "vscode", "jetbrains"])
    reg.add_argument("--workspace", type=Path, default=None)
    reg.add_argument("--transport", choices=["http", "stdio"],
                     default="http",
                     help="server entry type — http (default): the daemon's "
                          "localhost endpoint; the IDE reconnects per request "
                          "so a dead or suspended session never needs a "
                          "reload (#206). stdio: per-window child process")
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
        from confluence_knowledge.update import run_update
        sys.exit(run_update(
            "local-knowledge", cli_module="local_knowledge.cli_local",
            home=home(), server_module="local_knowledge.server_local",
            to=args.to, latest=args.latest, rollback=args.rollback,
            engine_dist="confluence-knowledge"))
    if args.cmd == "selftest":
        from confluence_knowledge.selftest import run
        rep = run(home(), "local_knowledge.server_local")
        print(__import__("json").dumps(rep, indent=2))
        sys.exit(0 if rep["ok"] else 1)
    if args.cmd == "register":
        from confluence_knowledge.registration import register
        try:
            wd = (Path(os.environ[WORKDIR_ENV]).expanduser().resolve()
                  if os.environ.get(WORKDIR_ENV) else None)
            if args.transport == "http":
                from confluence_knowledge.rpc import RpcDb, ensure_daemon
                from confluence_knowledge.settings import daemon_home
                wd = daemon_home()
                ensure_daemon(RpcDb(wd / "rpc.db"))
            results = register(ide=args.ide, workspace=args.workspace,
                               transport=args.transport,
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
