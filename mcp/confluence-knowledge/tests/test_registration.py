"""register: writes/removes our entry in every config Copilot reads."""

import json

import pytest

from confluence_knowledge import registration as reg


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "win"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() consults USERPROFILE on Windows, not HOME — mock it so
    # the windsurf/cursor probes don't read the runner's real profile
    from pathlib import Path
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    yield tmp_path


def test_targets_cover_jetbrains_and_workspace(sandbox):
    ws = sandbox / "repo"
    ws.mkdir()
    files = reg.targets("all", ws)
    assert any("github-copilot" in str(f) and "intellij" in str(f) for f in files)
    assert ws / ".vscode" / "mcp.json" in files


def test_register_writes_venv_command_and_merges(sandbox):
    ws = sandbox / "repo"
    (ws / ".vscode").mkdir(parents=True)
    existing = ws / ".vscode" / "mcp.json"
    existing.write_text(json.dumps({"servers": {"other": {"command": "x"}},
                                    "settings": {"a": 1}}))

    results = reg.register("all", ws)
    files = [f for f, _ in results]
    jb = files[0]
    data = json.loads(jb.read_text())
    entry = data["servers"]["knowledge"]
    assert entry["command"].endswith(
        ("python", "python.exe", "pythonw.exe"))
    assert entry["args"] == ["-m", "confluence_knowledge.unified"]

    merged = json.loads(existing.read_text())
    assert merged["servers"]["other"] == {"command": "x"}
    assert merged["settings"] == {"a": 1}
    assert "knowledge" in merged["servers"]


def test_register_backs_up_unparseable_config(sandbox):
    ws = sandbox / "repo"
    (ws / ".vscode").mkdir(parents=True)
    f = ws / ".vscode" / "mcp.json"
    f.write_text('// comment\n{"servers": {}}')  # JSONC — plain json can't parse
    reg.register("vscode", ws)
    data = json.loads(f.read_text())
    assert "knowledge" in data["servers"]
    assert (f.with_suffix(".json.bak")).exists()


def test_remove_and_registered_paths(sandbox):
    reg.register("jetbrains")
    assert reg.registered_paths("knowledge")
    reg.register("jetbrains", remove=True)
    assert not reg.registered_paths("knowledge")


def test_no_targets_errors_cleanly(sandbox, monkeypatch):
    monkeypatch.setattr(reg, "targets", lambda ide, ws=None: [])
    from confluence_knowledge.settings import SetupError
    with pytest.raises(SetupError, match="no IDE config locations"):
        reg.register("all")


# ---- #257 — Windsurf + Cursor use the mcpServers root key ----

def test_targets_windsurf_and_cursor(sandbox):
    (sandbox / ".codeium" / "windsurf").mkdir(parents=True)
    (sandbox / ".cursor").mkdir()
    ws = sandbox / "repo"
    ws.mkdir()
    files = reg.targets("all", ws)
    assert sandbox / ".codeium" / "windsurf" / "mcp_config.json" in files
    assert sandbox / ".cursor" / "mcp.json" in files
    assert ws / ".cursor" / "mcp.json" in files
    # an absent Windsurf install drops out of auto targets
    (sandbox / ".codeium" / "windsurf").rename(sandbox / ".codeium" / "ws2")
    assert reg.targets("windsurf") == []


def test_windsurf_writes_under_mcpservers_key(sandbox):
    (sandbox / ".codeium" / "windsurf").mkdir(parents=True)
    reg.register("windsurf")
    data = json.loads((sandbox / ".codeium" / "windsurf"
                       / "mcp_config.json").read_text())
    assert "knowledge" in data["mcpServers"]
    assert "servers" not in data


def test_cursor_workspace_merges_mcpservers_and_gitignores(sandbox):
    ws = sandbox / "repo"
    (ws / ".cursor").mkdir(parents=True)
    f = ws / ".cursor" / "mcp.json"
    f.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    reg.register("cursor", ws)
    data = json.loads(f.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert "knowledge" in data["mcpServers"]
    assert ".cursor/mcp.json" in (ws / ".gitignore").read_text()


def test_registered_paths_sees_both_root_keys(sandbox):
    (sandbox / ".codeium" / "windsurf").mkdir(parents=True)
    reg.register("windsurf")
    assert reg.registered_paths("knowledge") == [
        sandbox / ".codeium" / "windsurf" / "mcp_config.json"]
    assert reg.registered_transport("knowledge") == "stdio"
