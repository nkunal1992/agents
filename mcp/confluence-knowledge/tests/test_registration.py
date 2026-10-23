"""register: writes/removes our entry in every config Copilot reads."""

import json

import pytest

from confluence_knowledge import registration as reg


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "win"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path))
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
    entry = data["servers"]["confluence-knowledge"]
    assert entry["command"].endswith(
        ("python", "python.exe", "pythonw.exe"))
    assert entry["args"] == ["-m", "confluence_knowledge.server"]

    merged = json.loads(existing.read_text())
    assert merged["servers"]["other"] == {"command": "x"}
    assert merged["settings"] == {"a": 1}
    assert "confluence-knowledge" in merged["servers"]


def test_register_backs_up_unparseable_config(sandbox):
    ws = sandbox / "repo"
    (ws / ".vscode").mkdir(parents=True)
    f = ws / ".vscode" / "mcp.json"
    f.write_text('// comment\n{"servers": {}}')  # JSONC — plain json can't parse
    reg.register("vscode", ws)
    data = json.loads(f.read_text())
    assert "confluence-knowledge" in data["servers"]
    assert (f.with_suffix(".json.bak")).exists()


def test_remove_and_registered_paths(sandbox):
    reg.register("jetbrains")
    assert reg.registered_paths()
    reg.register("jetbrains", remove=True)
    assert not reg.registered_paths()


def test_no_targets_errors_cleanly(sandbox, monkeypatch):
    monkeypatch.setattr(reg, "targets", lambda ide, ws=None: [])
    from confluence_knowledge.settings import SetupError
    with pytest.raises(SetupError, match="no IDE config locations"):
        reg.register("all")
