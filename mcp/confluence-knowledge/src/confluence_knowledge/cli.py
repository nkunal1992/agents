"""confluence-knowledge CLI: setup / doctor / prepare-models / serve / clean.

The setup wizard prompts for tokens via getpass in the user's own terminal —
tokens never travel through chat or command-line arguments.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from .auth import store_credential
from .client import AccessError, make_client
from .retrieval import prepare_models
from .settings import AUTH_FILE_NAME, SetupError, SiteProfile, default_home, ensure_gitignore


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

    workdir = Path.cwd()
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
    try:
        s = status()
        print(json.dumps(s, indent=2))
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
    if args.cmd == "serve":
        from .server import main as serve
        serve()
        return
    if args.cmd == "clean":
        import shutil

        from .settings import INDEX_DIR_NAME
        target = Path.cwd() / INDEX_DIR_NAME
        if target.exists() and (args.yes or input(
                f"delete {target}? [y/N] ").strip().lower() == "y"):
            shutil.rmtree(target)
            print("index deleted")
        return


if __name__ == "__main__":
    main()
