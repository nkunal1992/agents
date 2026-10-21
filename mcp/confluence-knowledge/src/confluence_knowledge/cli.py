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


def _mask(token: str) -> str:
    """Masked confirmation — never echo the full secret (#191)."""
    t = token.strip()
    return t[:4] + "…" + t[-4:] if len(t) > 10 else "…"


def _looks_like_token(token: str) -> bool:
    """Cheap shape check before touching the network — clipboard may hold
    anything. Confluence PATs/API tokens are long, opaque, ASCII."""
    t = token.strip()
    return len(t) >= 16 and t.isprintable() and "\n" not in t and " " not in t


def _clipboard() -> str:
    """Read the OS clipboard — macOS/Linux/Windows. The token takes the
    zero-chat path: copy once, run one command, never typed (#191)."""
    import shutil
    import subprocess
    if shutil.which("pbpaste"):                    # macOS
        return subprocess.run(["pbpaste"], check=False, capture_output=True,
                              text=True, timeout=5).stdout
    if shutil.which("wl-paste"):                   # wayland
        return subprocess.run(["wl-paste", "-n"], check=False, capture_output=True,
                              text=True, timeout=5).stdout
    if shutil.which("xclip"):                      # x11
        return subprocess.run(["xclip", "-o", "-selection", "clipboard"], check=False,
                              capture_output=True, text=True,
                              timeout=5).stdout
    if shutil.which("powershell.exe") or shutil.which("pwsh"):  # windows
        cmd = shutil.which("powershell.exe") or shutil.which("pwsh")
        return subprocess.run([cmd, "-NoProfile", "-Command",
                               "Get-Clipboard"],
                              check=False, capture_output=True, text=True, timeout=8).stdout
    raise SetupError("no clipboard tool found (pbpaste/wl-paste/xclip/"
                     "powershell) — run `setup` without --from-clipboard "
                     "for the hidden-prompt path")


def _heartbeat(env: str, base: str, email: str | None, ref: str,
               token: str) -> str:
    """Validate the token against the API BEFORE persisting (#191 —
    never store a credential that fails its first real call)."""
    c = make_client(SiteProfile(env, base, email, ref), token)
    ep = "wiki/rest/api/user/current" if env == "cloud" else "rest/api/user/current"
    who = c.get(ep).json()
    return who.get("displayName") or who.get("username") or "OK"


def _persist_and_report(env: str, base: str, email: str | None,
                        token: str) -> int:
    """Validate → persist → masked confirm. Shared by both token paths."""
    workdir = resolve_workdir()
    workdir.mkdir(parents=True, exist_ok=True)
    home = default_home()
    try:
        ensure_gitignore(workdir, [AUTH_FILE_NAME, ".conf_knowledge_index/"])
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    ref = f"{env}:{base}:{email or '-'}"
    try:
        who = _heartbeat(env, base, email, ref, token)
    except Exception as e:  # noqa: BLE001 — auth/network/TLS all mean no
        print(f"connection check failed — nothing stored: {e}",
              file=sys.stderr)
        return 3
    where = store_credential(workdir, home, ref, token)
    SiteProfile(env, base, email, ref).save(home)
    print(f"\nconnected as: {who}")
    print(f"token received: {_mask(token)}  (stored: {where})")
    print(f"profile: {home / 'profile.json'}")
    print("\nNext: `confluence-knowledge prepare-models` to fetch the local "
          "models (Apache-2.0), then point your IDE's MCP config at:\n"
          f"  command: {sys.executable}\n"
          f"  args:    [\"-m\", \"confluence_knowledge.server\"]\n"
          f"  env:     CONF_KNOWLEDGE_WORKDIR={workdir}")
    return 0


def register_clipboard_credential(env: str, site: str,
                                email: str | None) -> dict:
    """Clipboard → validate → persist → masked confirm, as data.

    Same pipeline as `setup --from-clipboard` but returns a dict so the
    MCP `register_token` tool can offer the zero-chat path without a
    shell command (#191). The token is read from the OS clipboard inside
    this process — it is never a tool argument (must not transit the
    chat model or land in chat history)."""
    base = SiteProfile.canonical_base(env, site)
    token = _clipboard().strip()
    if not token:
        raise SetupError("clipboard is empty — copy the token first")
    if not _looks_like_token(token):
        raise SetupError("clipboard doesn't look like a token — nothing "
                         "stored (copied the right thing?)")
    workdir = resolve_workdir()
    workdir.mkdir(parents=True, exist_ok=True)
    home = default_home()
    ensure_gitignore(workdir, [AUTH_FILE_NAME, INDEX_DIR_NAME])
    ref = f"{env}:{base}:{email or '-'}"
    who = _heartbeat(env, base, email, ref, token)   # raises — nothing stored
    where = store_credential(workdir, home, ref, token)
    SiteProfile(env, base, email, ref).save(home)
    return {"configured": True, "connected_as": who,
            "token_received": _mask(token), "stored": where,
            "profile": str(home / "profile.json"),
            "note": "credentials live — the daemon picks them up on the "
                    "next call; no reload needed"}


def _setup_noninteractive(args) -> int:
    """Clipboard / flag-driven setup — the chat-side seamless path (#191).
    The token arrives via clipboard or stdin only — never via a CLI arg
    or MCP tool argument (it must not transit the chat model)."""
    env = args.env or "cloud"
    try:
        base = SiteProfile.canonical_base(env, args.site)
    except SetupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    email = args.email if env == "cloud" else None
    if env == "cloud" and not email:
        print("error: cloud sites need --email", file=sys.stderr)
        return 2
    if args.from_clipboard:
        token = _clipboard().strip()
    elif args.token_stdin:
        token = sys.stdin.read().strip()
    else:                                           # pragma: no cover
        print("error: pass --from-clipboard or --token-stdin",
              file=sys.stderr)
        return 2
    if not token:
        print("error: empty token (clipboard empty?)", file=sys.stderr)
        return 2
    if not _looks_like_token(token):
        print("error: clipboard doesn't look like a token — nothing "
              "stored", file=sys.stderr)
        return 2
    return _persist_and_report(env, base, email, token)


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

    token = getpass.getpass("Token (input hidden): ")
    if not token:
        print("error: empty token", file=sys.stderr)
        return 2

    return _persist_and_report(env, base, email, token)


def _doctor() -> int:
    from .server import status
    from .update import check
    try:
        s = status()
        upd = check("confluence-knowledge")
        if upd.get("installed_yanked"):
            s.setdefault("checks", []).append({
                "flag": "release", "ok": False,
                "detail": f"installed {upd['installed']} was yanked"
                          + (f" ({upd['yank_reason']})"
                             if upd.get("yank_reason") else ""),
                "remediation": "confluence-knowledge update "
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
        if upd["update_available"]:
            print("update available: run `confluence-knowledge update` "
                  "(soaked release, self-tested, rolls back on failure)")
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
        return 4 if bad else 0
    except SetupError as e:
        print(f"setup error: {e}", file=sys.stderr)
        return 2


def main() -> None:
    ap = argparse.ArgumentParser(prog="confluence-knowledge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("setup", help="guided credential + config setup")
    st.add_argument("--site", help="Confluence URL — skips the prompt")
    st.add_argument("--email", help="Atlassian account email (cloud)")
    st.add_argument("--env", choices=["cloud", "datacenter"],
                    help="environment — skips the prompt")
    st.add_argument("--from-clipboard", action="store_true",
                    help="read the token from the OS clipboard — the "
                         "zero-chat path (#191): the secret never transits "
                         "the chat model or history")
    st.add_argument("--token-stdin", action="store_true",
                    help="read the token from stdin (pipe it — never a "
                         "command-line arg)")
    sub.add_parser("doctor", help="verify config, credentials, models")
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
        if args.from_clipboard or args.token_stdin or args.site:
            sys.exit(_setup_noninteractive(args))
        sys.exit(_wizard())
    if args.cmd == "doctor":
        sys.exit(_doctor())
    if args.cmd == "prepare-models":
        for line in prepare_models(default_home()):
            print(line)
        return
    if args.cmd == "update":
        from .update import run_update
        sys.exit(run_update(
            "confluence-knowledge", cli_module="confluence_knowledge.cli",
            home=default_home(), server_module="confluence_knowledge.server",
            to=args.to, latest=args.latest, rollback=args.rollback))
    if args.cmd == "selftest":
        from .selftest import run
        rep = run(default_home(), "confluence_knowledge.server")
        print(__import__("json").dumps(rep, indent=2))
        sys.exit(0 if rep["ok"] else 1)
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
