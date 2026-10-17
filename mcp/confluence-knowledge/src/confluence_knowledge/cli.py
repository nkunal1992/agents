"""confluence-knowledge CLI: setup / doctor / prepare-models / serve / clean.

The setup wizard prompts for tokens via getpass in the user's own terminal —
tokens never travel through chat or command-line arguments.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .auth import store_credential
from .client import AccessError, make_client
from .retrieval import prepare_models
from .settings import (
    AUTH_FILE_NAME,
    INDEX_DIR_NAME,
    SetupError,
    SiteProfile,
    default_home,
    ensure_gitignore,
    resolve_workdir,
)


def _wizard() -> int:
    print("confluence-knowledge setup\n")
    env = ""
    while env not in ("1", "2"):
        env = input("Confluence environment — [1] Cloud (atlassian.net) "
                    "or [2] Data Center (corporate URL)? ").strip()
    env = "cloud" if env == "1" else "datacenter"

    if env == "cloud":
        print("\nCreate a token: sign in to https://id.atlassian.com → "
              "Security → Create and manage API tokens → Create API token "
              "(choose Confluence read scopes if scoped tokens are offered).")
        site = input("\nSite URL (e.g. https://acme.atlassian.net): ").strip()
        email = input("Atlassian account email: ").strip()
    else:
        print("\nCreate a token: Confluence → your profile picture → Settings → "
              "Personal Access Tokens → Create token (read access is enough).")
        site = input("\nSite URL incl. context path (e.g. https://conf.acme.corp/confluence): ").strip()
        email = None

    try:
        base = SiteProfile.canonical_base(env, site)
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    workdir = resolve_workdir()
    workdir.mkdir(parents=True, exist_ok=True)
    home = default_home()
    # gitignore protections BEFORE any secret/cache write
    try:
        ensure_gitignore(workdir, [AUTH_FILE_NAME, ".conf_knowledge_index/"])
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    ref = f"{env}:{base}:{email or '-'}"
    token = getpass.getpass("Token (input hidden): ")
    if not token:
        print("error: empty token", file=sys.stderr)
        return 2

    where = store_credential(workdir, home, ref, token)
    SiteProfile(env, base, email, ref).save(home)

    # heartbeat
    try:
        c = make_client(SiteProfile(env, base, email, ref), token)
        ep = "wiki/rest/api/user/current" if env == "cloud" else "rest/api/user/current"
        who = c.get(ep).json()
        print(f"\nconnected as: {who.get('displayName') or who.get('username') or 'OK'}")
    except Exception as e:  # noqa: BLE001 — any connect failure (auth/network/TLS) exits non-zero
        print(f"\nconnection check failed: {e}", file=sys.stderr)
        return 3

    print(f"credential stored: {where}")
    print(f"profile: {home / 'profile.json'}")
    print("\nNext: `confluence-knowledge prepare-models` to fetch the local "
          "models (Apache-2.0), then point your IDE's MCP config at:\n"
          f"  command: {sys.executable}\n"
          f"  args:    [\"-m\", \"confluence_knowledge.server\"]\n"
          f"  env:     CONF_KNOWLEDGE_WORKDIR={workdir}")
    return 0


def _doctor() -> int:
    from .server import status
    from .update import check
    try:
        s = status()
        print(json.dumps(s, indent=2))
        upd = check("confluence-knowledge")
        if upd["installed"]:
            line = f"version: {upd['installed']}"
            if upd["latest"]:
                line += f" (latest: {upd['latest']})"
            elif upd["latest"] is None:
                line += " (latest: unchecked — offline or internal mirror)"
            print(line)
        if upd["update_available"]:
            print("update available: run `confluence-knowledge update` "
                  "(or pip install -U confluence-knowledge[models])")
        if s.get("configured"):
            from .server import _client_for  # internal but fine in CLI
            profile = SiteProfile.load(default_home())
            try:
                c = _client_for(profile)
                ep = ("wiki/rest/api/user/current" if profile.env == "cloud"
                      else "rest/api/user/current")
                who = c.get(ep).json()
                print("heartbeat: OK —",
                      who.get("displayName") or who.get("username"))
            except (AccessError, SetupError) as e:
                print(f"heartbeat: FAILED — {e}", file=sys.stderr)
                return 3
        return 0
    except SetupError as e:
        print(f"setup error: {e}", file=sys.stderr)
        return 2


def main() -> None:
    ap = argparse.ArgumentParser(prog="confluence-knowledge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="guided credential + config setup")
    sub.add_parser("doctor", help="verify config, credentials, models")
    sub.add_parser("prepare-models", help="download the pinned local models")
    sub.add_parser("update", help="upgrade this package via pip -U")
    reg = sub.add_parser(
        "register",
        help="write this server into the IDE's MCP config (no manual paste)")
    reg.add_argument("--ide", default="auto",
                     choices=["auto", "all", "vscode", "jetbrains"],
                     help="which config files to write (default: all that apply)")
    reg.add_argument("--workspace", type=Path, default=None,
                     help="also write <workspace>/.vscode/mcp.json")
    reg.add_argument("--remove", action="store_true",
                     help="remove our entry instead of adding it")
    sub.add_parser("serve", help="run the stdio MCP server")
    clean = sub.add_parser("clean", help="delete the local index for this repo")
    clean.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if args.cmd == "setup":
        sys.exit(_wizard())
    if args.cmd == "doctor":
        sys.exit(_doctor())
    if args.cmd == "prepare-models":
        for line in prepare_models(default_home()):
            print(line)
        return
    if args.cmd == "update":
        import subprocess
        cmd = [sys.executable, "-m", "pip", "install", "-U",
               "confluence-knowledge[models]"]
        if os.name == "nt":
            # this exe is the running process on Windows — pip can't replace
            # a file that's in use; hand the user the equivalent python -m
            # pip command instead (same mechanism `pip install -U pip` uses)
            print("on Windows, upgrade via the venv python directly:\n  "
                  + " ".join(f'"{c}"' if " " in c else c for c in cmd))
            return
        r = subprocess.run(cmd, check=False)
        if r.returncode == 0:
            print("\nupdated — run `confluence-knowledge prepare-models` "
                  "(new releases may pin new model artifacts) then "
                  "`confluence-knowledge doctor`")
        sys.exit(r.returncode)
    if args.cmd == "register":
        from .registration import register
        try:
            wd = (Path(os.environ["CONF_KNOWLEDGE_WORKDIR"]).expanduser().resolve()
                  if os.environ.get("CONF_KNOWLEDGE_WORKDIR") else None)
            results = register(ide=args.ide, workspace=args.workspace,
                               workdir=wd, remove=args.remove)
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
        from .server import main as serve
        serve()
        return
    if args.cmd == "clean":
        import shutil

        target = resolve_workdir() / INDEX_DIR_NAME
        if target.exists() and (args.yes or input(
                f"delete {target}? [y/N] ").strip().lower() == "y"):
            shutil.rmtree(target)
            print("index deleted")
        return


if __name__ == "__main__":
    main()
