"""sharepoint-knowledge CLI — setup (Entra device sign-in), doctor,
prepare-models, register, update, serve, clean.

Same shape as the other knowledge CLIs. `setup` prints the exact Entra
app-registration steps for IT, collects tenant/client/site, then runs the
one-time device-code sign-in.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from confluence_knowledge.registration import register as _register
from confluence_knowledge.retrieval import prepare_models
from confluence_knowledge.settings import SetupError, ensure_gitignore

from .settings_sp import (
    APP_NAME,
    AUTH_ENV,
    AUTH_FILE_NAME,
    INDEX_DIR_NAME,
    PROFILE_NAME,
    SPProfile,
    home,
    workdir,
)

TOKEN_STEPS = """\
Entra app registration — once per tenant, done by IT (or you, if allowed):
  1. Entra portal (entra.microsoft.com) → Applications → App registrations
     → New registration
  2. Name: e.g. "sharepoint-knowledge-agent" — Accounts in this org only
  3. Authentication → Enable "Allow public client flows" → YES (required
     for device-code sign-in — no client secret is used or needed)
  4. API permissions → Add → Microsoft Graph → Delegated → Sites.Read.All
     (+ offline_access is implied by device flow)
  5. Grant admin consent for the tenant (the delegated scope normally
     requires it) — one click on the Permissions page
  6. Copy: Directory (tenant) ID and Application (client) ID — needed below

This app requests only delegated read access — it can read exactly what
the signed-in user can already read, nothing more.
"""


def _setup() -> int:
    print("SharePoint Knowledge — first-run setup\n")
    print(TOKEN_STEPS)

    tenant = input("Tenant ID (GUID): ").strip()
    client = input("Client ID (application ID, GUID): ").strip()
    site = input("SharePoint site URL (e.g. https://contoso.sharepoint.com"
                 "/sites/team): ").strip().rstrip("/")
    if not (tenant and client and site):
        print("tenant ID, client ID, and site URL are all required",
              file=sys.stderr)
        return 1

    profile = SPProfile(tenant_id=tenant, client_id=client,
                        site_url=site, credential_ref="sp-token")
    profile.save()
    print(f"\nprofile saved to {home() / PROFILE_NAME}\n")

    print("Sign-in (device code — one browser step). The code you get "
          "never leaves the OS credential store.", flush=True)
    if os.environ.get(AUTH_ENV):
        print("SP_KNOWLEDGE_TOKEN is set — using it; skipping sign-in")
    else:
        from .auth_sp import device_sign_in
        try:
            r = device_sign_in(profile, workdir(), home())
            print(f"signed in as {r['user']} — token cache stored at "
                  f"{r['where']}")
        except SetupError as e:
            print(f"sign-in failed: {e}", file=sys.stderr)
            return 1

    # make sure the workdir's gitignore hides the index + auth file
    workdir().mkdir(parents=True, exist_ok=True)
    ensure_gitignore(workdir(), [AUTH_FILE_NAME,
                                 f"{INDEX_DIR_NAME}/"])

    if input("\nDownload the local models now (~1.3 GB, one-time)? "
             "[Y/n] ").strip().lower() not in ("n", "no"):
        for line in prepare_models(home()):
            print(line)

    print("\nsetup complete — run `sharepoint-knowledge doctor` to verify")
    return 0


def _doctor() -> int:
    from confluence_knowledge.update import check

    from .server_sp import status
    s = status()
    print(json.dumps(s, indent=2))
    upd = check("sharepoint-knowledge")
    if upd["installed"]:
        line = f"version: {upd['installed']}"
        if upd["latest"]:
            line += f" (latest: {upd['latest']})"
        elif upd["latest"] is None:
            line += " (latest: unchecked — offline or internal mirror)"
        print(line)
    if upd["update_available"]:
        print("update available: run `sharepoint-knowledge update` "
              "(or pip install -U sharepoint-knowledge[models])")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="sharepoint-knowledge",
                                description="Local SharePoint indexer + "
                                            "grounded-Q&A MCP service")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="configure Entra sign-in + site")
    sub.add_parser("doctor", help="report configuration + model readiness")
    sub.add_parser("prepare-models", help="download the pinned local models")
    sub.add_parser("update", help="upgrade this package via pip -U")
    reg = sub.add_parser("register",
                         help="write the IDE MCP config automatically")
    reg.add_argument("--ide", choices=["auto", "all", "jetbrains", "vscode"],
                     default="auto")
    reg.add_argument("--workspace", default=None,
                     help="repo dir for the workspace .vscode/mcp.json")
    reg.add_argument("--remove", action="store_true",
                     help="remove our server entry instead of adding it")
    sub.add_parser("serve", help="run the stdio MCP server")
    sub.add_parser("clean", help="delete all indexed data")

    args = p.parse_args()
    if args.cmd == "setup":
        sys.exit(_setup())
    if args.cmd == "doctor":
        sys.exit(_doctor())
    if args.cmd == "prepare-models":
        for line in prepare_models(home()):
            print(line)
        return
    if args.cmd == "update":
        import subprocess
        cmd = [sys.executable, "-m", "pip", "install", "-U",
               "sharepoint-knowledge[models]"]
        if os.name == "nt":
            print("on Windows, upgrade via the venv python directly:\n  "
                  + " ".join(f'"{c}"' if " " in c else c for c in cmd))
            return
        r = subprocess.run(cmd, check=False)
        if r.returncode == 0:
            print("\nupdated — run `sharepoint-knowledge prepare-models` "
                  "(new releases may pin new model artifacts) then "
                  "`sharepoint-knowledge doctor`")
        sys.exit(r.returncode)
    if args.cmd == "register":
        results = _register(
            ide=args.ide,
            workspace=Path(args.workspace) if args.workspace else None,
            remove=args.remove,
            server_id=APP_NAME,
            module="sharepoint_knowledge.server_sp",
            env_var="SP_KNOWLEDGE_WORKDIR")
        for path, action in results:
            print(f"{action}: {path}")
        if args.remove:
            print("removed — reload the IDE to drop the server")
        else:
            print("registered — reload the IDE so the tools appear")
        return
    if args.cmd == "serve":
        from .server_sp import main as serve
        serve()
        return
    if args.cmd == "clean":
        import shutil

        from .settings_sp import index_root
        d = index_root()
        if d.exists():
            shutil.rmtree(d)
            print(f"deleted {d}")
        else:
            print("nothing to clean")
        return


if __name__ == "__main__":
    main()
