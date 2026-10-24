"""IDE MCP registration — writes the config files itself.

The agent can't click IDE settings for the user, but it CAN run this command:
we write the mcp.json files directly wherever Copilot looks. The command path
is `sys.executable` — always the venv interpreter that has the package.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .settings import SetupError, ensure_gitignore

SERVER_ID = "confluence-knowledge"
SERVER_MODULE = "confluence_knowledge.server"
WORKDIR_ENV = "CONF_KNOWLEDGE_WORKDIR"


def _server_entry(workdir: Path | None, module: str, env_var: str,
                  transport: str = "stdio",
                  server_id: str = SERVER_ID) -> dict:
    if transport == "http":
        # daemon-hosted endpoint — the client reconnects per request, so a
        # dead/suspended session never needs an IDE reload (#206). Each
        # service mounts under /<server_id>/ on the shared daemon. Falls
        # back to stdio when the daemon's http.json isn't reachable.
        meta = _http_meta(workdir)
        if meta:
            # ?token= as well as the header — JetBrains Copilot silently
            # drops mcp.json headers (upstream #1579); the daemon accepts
            # either. The token already lives in this file either way.
            return {"type": "http",
                    "url": f"http://127.0.0.1:{meta['port']}/"
                           f"{server_id}/mcp?token={meta['token']}",
                    "headers": {"Authorization": f"Bearer {meta['token']}"}}
    exe = sys.executable            # the venv python — no PATH guessing
    if sys.platform == "win32":
        # pythonw.exe has no console — a python.exe proxy per IDE window
        # would pop a terminal per spawn on Windows (#226)
        wexe = Path(exe).with_name("pythonw.exe")
        if wexe.exists():
            exe = str(wexe)
    entry = {
        "command": exe,
        "args": ["-m", module],
    }
    if workdir is not None:
        entry["env"] = {env_var: str(workdir)}
    return entry


def _daemon_services_version(meta: dict) -> str | None:
    """The running daemon's package version from /_services — None when
    unreachable or the field is absent (pre-#226 daemons report only
    services). Loopback URL is built from our own http.json."""
    try:
        import json as _json
        import urllib.request as _req

        from .rpc import _local_urlopen
        req = _req.Request(
            f"http://127.0.0.1:{meta['port']}/_services",
            headers={"Authorization": f"Bearer {meta['token']}"})
        if not req.full_url.startswith("http://127.0.0.1:"):
            return None
        with _local_urlopen(req, timeout=2) as r:   # nosec B310
            return _json.loads(r.read()).get("version")
    except Exception:                               # noqa: BLE001
        return None


def daemon_http_status(cli: str = "confluence-knowledge") -> dict:
    """The daemon_http health check, shared by every service's status —
    one implementation so the flags can't drift. `ok=False` covers both
    'endpoint down' and 'daemon running older code than the installed
    package' (#226 — a live daemon after pip install -U still serves the
    old code until restarted). For stdio registrations the endpoint is
    optional — the proxy boots the daemon on demand, so a cold start
    (no http.json yet) is healthy, not a failure."""
    check = {"flag": "daemon_http", "ok": False,
             "detail": "no http endpoint — daemon down or pre-#206",
             "remediation": f"{cli} doctor (self-heals the daemon)"}
    meta = _http_meta()
    if not meta:
        if registered_transport(cli) == "stdio":
            check.update(ok=True, remediation=None,
                         detail=("no http endpoint — stdio entry: the "
                                 "daemon starts on the first call"))
        return check
    import socket as _sock
    try:
        with _sock.socket() as probe:
            probe.settimeout(2)
            probe.connect(("127.0.0.1", meta["port"]))
    except OSError:
        if registered_transport(cli) == "stdio":
            check.update(ok=True, remediation=None,
                         detail=(f"endpoint at 127.0.0.1:{meta['port']} "
                                 "down — stdio entry: the proxy respawns "
                                 "the daemon on the next call"))
        else:
            check["detail"] = (f"endpoint at 127.0.0.1:{meta['port']} "
                               "down — daemon dead or stale URL")
        return check
    check["detail"] = (f"mcp endpoint listening on "
                       f"127.0.0.1:{meta['port']}")
    check["ok"] = True
    dver = _daemon_services_version(meta)
    if dver and dver != "unknown":
        try:
            import importlib.metadata as _im
            installed = _im.version("confluence-knowledge")
        except Exception:                           # noqa: BLE001
            installed = None
        if installed and dver != installed:
            check.update(ok=False,
                         detail=(f"daemon running {dver}, installed "
                                 f"{installed} — restart picks up the fix"))
        else:
            check["detail"] += f" (v{dver})"
    return check


def _http_meta(workdir: Path | None = None) -> dict | None:
    """The daemon's {port, token} — written at daemon boot in the
    venv-keyed daemon home; absent means no HTTP endpoint is up."""
    import json as _json

    from .settings import daemon_home
    root = workdir if workdir is not None else daemon_home()
    try:
        meta = _json.loads((root / "http.json").read_text(encoding="utf-8"))
        if isinstance(meta.get("port"), int) and meta.get("token"):
            return meta
    except Exception:                           # noqa: BLE001,S110
        pass
    return None


def daemon_url() -> str:
    """This venv's daemon loopback base URL ('' when no endpoint is up) —
    surfaced in status() so two IDE installs that fork their daemon slots
    show visibly different values (#257)."""
    meta = _http_meta()
    return f"http://127.0.0.1:{meta['port']}/" if meta else ""


def _platform_appdata() -> Path | None:
    """Where per-app support data lives, per OS."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        return Path(base) if base else Path.home() / "AppData" / "Local"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def _jetbrains_mcp() -> Path:
    """The user-level config the JetBrains Copilot plugin actually reads —
    the one we verified on a real Windows machine during field testing."""
    return _platform_appdata() / "github-copilot" / "intellij" / "mcp.json"


def _vscode_user_mcp() -> Path | None:
    """User-level VS Code mcp.json — only if the profile dir exists."""
    for code in ("Code", "Code - Insiders", "VSCodium"):
        d = _platform_appdata() / code / "User"
        if d.is_dir():
            return d / "mcp.json"
    return None


def _workspace_mcp(workspace: Path | None) -> Path | None:
    return (workspace / ".vscode" / "mcp.json") if workspace else None


def _windsurf_mcp() -> Path | None:
    """Windsurf's user-level MCP config — only when the app is installed."""
    d = Path.home() / ".codeium" / "windsurf"
    return d / "mcp_config.json" if d.is_dir() else None


def _cursor_user_mcp() -> Path | None:
    """Cursor's user-level MCP config — only when ~/.cursor exists."""
    d = Path.home() / ".cursor"
    return d / "mcp.json" if d.is_dir() else None


def _cursor_workspace_mcp(workspace: Path | None) -> Path | None:
    return (workspace / ".cursor" / "mcp.json") if workspace else None


def _root_key(path: Path) -> str:
    """The JSON root an IDE reads its server table under — VS Code and
    JetBrains use `servers`; Windsurf and Cursor use `mcpServers` (#257)."""
    if path.name == "mcp_config.json" or ".cursor" in path.parts:
        return "mcpServers"
    return "servers"


def targets(ide: str = "auto", workspace: Path | None = None) -> list[Path]:
    """Every config file the chosen IDEs read. `auto` = all that apply."""
    out = []
    if ide in ("auto", "all", "jetbrains"):
        out.append(_jetbrains_mcp())
    if ide in ("auto", "all", "vscode"):
        v = _vscode_user_mcp()
        if v:
            out.append(v)
        w = _workspace_mcp(workspace)
        if w:
            out.append(w)
    if ide in ("auto", "all", "windsurf"):
        w = _windsurf_mcp()
        if w:
            out.append(w)
    if ide in ("auto", "all", "cursor"):
        u = _cursor_user_mcp()
        if u:
            out.append(u)
        w = _cursor_workspace_mcp(workspace)
        if w:
            out.append(w)
    # de-dupe (user + workspace can resolve to the same file in tests)
    seen, dedup = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            dedup.append(p)
    return dedup


def _merge(path: Path, entry: dict | None, server_id: str = SERVER_ID) -> str:
    """Write `entry` under servers[server_id], or remove it when None.

    Merges with existing JSON; backs up before overwriting. A file that isn't
    parseable (JSONC comments etc.) is backed up and replaced — the agent
    tells the user to re-add their other servers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    replaced = False
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
            replaced = True  # unparseable (JSONC etc.) — back up and rewrite
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    servers = data.setdefault(_root_key(path), {})
    if entry is None:
        servers.pop(server_id, None)
        action = "removed"
    else:
        servers[server_id] = entry
        action = "backup-replaced" if replaced else "written"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return action


def _mounted_services(meta: dict) -> list[str]:
    """Probe the daemon's /_services endpoint — registered mounts. An
    unreachable daemon means 'not mounted' — register's caller decides."""
    import json as _json
    import urllib.request
    try:
        from .rpc import _local_urlopen
        req = urllib.request.Request(
            f"http://127.0.0.1:{meta['port']}/_services",
            headers={"Authorization": f"Bearer {meta['token']}"})
        if not req.full_url.startswith("http://127.0.0.1:"):
            return []                          # refuse a tampered meta URL
        with _local_urlopen(req, timeout=3) as r:  # nosec B310
            return _json.loads(r.read()).get("services", [])
    except Exception:                           # noqa: BLE001 — probe is
        return []                                # best-effort


def _restart_daemon() -> None:
    """SIGTERM the daemon holding the lease so the next ensure_daemon
    respawns it with the freshly-installed service mounted (#206 —
    install-order: a daemon booted before this package can't serve it)."""
    import signal
    import sqlite3 as _sq
    import time as _t

    from .settings import daemon_home
    try:
        db = _sq.connect(daemon_home() / "rpc.db", timeout=5)
        row = db.execute("SELECT pid FROM daemon_lease").fetchone()
        db.close()
        if row and row[0]:
            os.kill(row[0], signal.SIGTERM)
            deadline = _t.monotonic() + 8
            while _t.monotonic() < deadline:
                # psutil liveness, never os.kill(pid,0) — on Windows that
                # is CTRL_C_EVENT (throws or terminates; #260), and a bare
                # pid check lies on pid reuse → require the cmdline too
                try:
                    import psutil

                    from .rpc import _cmdline_is
                    if not psutil.pid_exists(int(row[0])) or not \
                            _cmdline_is(int(row[0]),
                                        "confluence_knowledge.daemon"):
                        break
                except ImportError:
                    if os.name == "nt":
                        break       # unverifiable → don't loop forever
                    try:
                        os.kill(row[0], 0)  # managed-ok — POSIX-only
                    except OSError:         # fallback; on Windows it's
                        break               # CTRL_C_EVENT, never a probe
                _t.sleep(0.2)
    except Exception:                           # noqa: BLE001,S110 —
        pass                                     # best-effort restart


def ensure_mounted(service_name: str) -> bool:
    """True when the venv daemon serves `service_name` at its URL. If the
    daemon is up but predates this install, restart it once and re-probe —
    the respawn discovers the new entry point."""
    meta = _http_meta()
    if not meta:
        return False
    if service_name in _mounted_services(meta):
        return True
    _restart_daemon()
    import time as _t
    deadline = _t.monotonic() + 15
    while _t.monotonic() < deadline:
        meta2 = _http_meta()
        if meta2 and service_name in _mounted_services(meta2):
            return True
        _t.sleep(0.5)
    return False


def register(ide: str = "auto", workspace: Path | None = None,
             workdir: Path | None = None, remove: bool = False,
             server_id: str = SERVER_ID, module: str = SERVER_MODULE,
             env_var: str = WORKDIR_ENV,
             transport: str = "stdio") -> list[tuple[Path, str]]:
    """Write/remove our server entry in each target. Returns (path, action)."""
    if ide not in ("auto", "all", "vscode", "jetbrains", "windsurf",
                   "cursor"):
        raise SetupError(f"unknown --ide '{ide}' "
                         "(auto|all|vscode|jetbrains|windsurf|cursor)")
    files = targets(ide, workspace)
    if not files:
        raise SetupError("no IDE config locations found — no Copilot/JetBrains "
                         "or VS Code profile detected")
    if transport == "http" and not remove and not ensure_mounted(server_id):
        # never write a dead URL — a daemon that won't mount this service
        # gets the stdio fallback plus a loud fix path (#206)
        import sys as _sys
        print(f"warning: {server_id} is not mounted on the shared daemon "
              f"— writing the stdio entry instead; run '<cli> doctor' to "
              f"repair the http endpoint", file=_sys.stderr)
        transport = "stdio"
    entry = None if remove else _server_entry(
        workdir, module, env_var, transport=transport,
        server_id=server_id)
    out = []
    for f in files:
        # a workspace-scoped mcp.json lands inside the user's repo — guard
        # it (and its backup/tmp siblings, which may carry OTHER servers'
        # config or secrets) from `git add .` via .gitignore +
        # .git/info/exclude (#192). Applies on remove too — _merge still
        # writes a .bak of the previous file.
        if f.name == "mcp.json" and f.parent.name in (".vscode", ".cursor"):
            d = f.parent.name
            ensure_gitignore(f.parent.parent,
                             [f"{d}/mcp.json", f"{d}/mcp.json.bak",
                              f"{d}/mcp.json.tmp"],
                             protected=())
        out.append((f, _merge(f, entry, server_id)))
    return out


def registered_transport(server_id: str = SERVER_ID) -> str | None:
    """The transport our registered entry currently uses — 'url' means
    http, 'command' means stdio. Doctor's self-heal rewrites the entry
    with the SAME type so an http user's URL refreshes while a stdio
    user's choice is preserved (#226)."""
    for p in targets("all"):
        try:
            entry = (json.loads(p.read_text(encoding="utf-8"))
                     .get(_root_key(p), {}).get(server_id))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict):
            return "http" if "url" in entry else "stdio"
    return None


def registered_paths(server_id: str = SERVER_ID,
                     workspace: Path | None = None) -> list[Path]:
    """Existing config files that already contain our server — for doctor.
    `workspace` also probes workspace-scoped mcp.json files (a
    `register --ide vscode --workspace <repo>` leaves no user-level trace)."""
    out = []
    probes = targets("all")
    if workspace:
        probes += [_workspace_mcp(p) for p in
                   {workspace, workspace / ".vscode" if workspace.name != ".vscode"
                     else workspace.parent}]
    for p in dict.fromkeys(q for q in probes if q):
        try:
            servers = json.loads(p.read_text(encoding="utf-8")).get(_root_key(p), {})
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if p.exists() and server_id in servers:
            out.append(p)
    return out
