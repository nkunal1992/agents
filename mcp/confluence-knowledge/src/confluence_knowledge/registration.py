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


def _server_entry(workdir: Path | None, module: str, env_var: str) -> dict:
    entry = {
        "command": sys.executable,  # the venv python — no PATH guessing
        "args": ["-m", module],
    }
    if workdir is not None:
        entry["env"] = {env_var: str(workdir)}
    return entry


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
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {}
            replaced = True  # unparseable (JSONC etc.) — back up and rewrite
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    servers = data.setdefault("servers", {})
    if entry is None:
        servers.pop(server_id, None)
        action = "removed"
    else:
        servers[server_id] = entry
        action = "backup-replaced" if replaced else "written"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)
    return action


def register(ide: str = "auto", workspace: Path | None = None,
             workdir: Path | None = None, remove: bool = False,
             server_id: str = SERVER_ID, module: str = SERVER_MODULE,
             env_var: str = WORKDIR_ENV) -> list[tuple[Path, str]]:
    """Write/remove our server entry in each target. Returns (path, action)."""
    if ide not in ("auto", "all", "vscode", "jetbrains"):
        raise SetupError(f"unknown --ide '{ide}' (auto|all|vscode|jetbrains)")
    files = targets(ide, workspace)
    if not files:
        raise SetupError("no IDE config locations found — no Copilot/JetBrains "
                         "or VS Code profile detected")
    entry = None if remove else _server_entry(workdir, module, env_var)
    out = []
    for f in files:
        # a workspace-scoped mcp.json lands inside the user's repo — guard
        # it (and its backup/tmp siblings, which may carry OTHER servers'
        # config or secrets) from `git add .` via .gitignore +
        # .git/info/exclude (#192). Applies on remove too — _merge still
        # writes a .bak of the previous file.
        if f.name == "mcp.json" and f.parent.name == ".vscode":
            ensure_gitignore(f.parent.parent,
                             [".vscode/mcp.json", ".vscode/mcp.json.bak",
                              ".vscode/mcp.json.tmp"],
                             protected=())
        out.append((f, _merge(f, entry, server_id)))
    return out


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
            servers = json.loads(p.read_text()).get("servers", {})
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if p.exists() and server_id in servers:
            out.append(p)
    return out
