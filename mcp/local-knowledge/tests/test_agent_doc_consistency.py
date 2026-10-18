"""The local-knowledge agent file must only reference CLI commands that exist."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
AGENT = REPO / "agents" / "local-knowledge.agent.md"


def test_agent_references_only_real_subcommands():
    if not AGENT.exists():
        import pytest
        pytest.skip("agent file not in this checkout layout")
    src = (Path(__file__).parents[1] / "src" /
           "local_knowledge" / "cli_local.py").read_text()
    real = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', src))
    doc = AGENT.read_text()
    # prose like "the local-knowledge service isn't responding" is not a
    # subcommand — the excluded words below are nouns, not commands
    referenced = set(re.findall(r"local-knowledge[` ]+([a-z-]+)", doc)) - \
        {"doctor", "-", "exe", "service", "agent", "knowledge", "bundle"}
    for cmd in referenced:
        assert cmd in real, \
            f"agent references `local-knowledge {cmd}` — not a real subcommand"


def test_agent_references_repo_files_that_exist():
    doc = AGENT.read_text()
    for m in re.findall(r"prompts/([\w.-]+)", doc):
        assert (REPO / "prompts" / m).exists(), \
            f"agent references missing prompts/{m}"


def test_agent_frontmatter_tools_exist():
    """Every tool named in the agent's frontmatter must be a real
    @mcp.tool() in the server source."""
    if not AGENT.exists():
        import pytest
        pytest.skip("agent file not in this checkout layout")
    doc = AGENT.read_text()
    tools_line = re.search(r'tools:\s*\[([^\]]+)\]', doc).group(1)
    app = AGENT.stem.replace(".agent", "")
    declared = {t.strip().strip('"').split("/")[1]
                for t in tools_line.split(",") if f"{app}/" in t}
    srv_name = {"local-knowledge": "server_local",
                "sharepoint-knowledge": "server_sp",
                "confluence-knowledge": "server"}[app]
    pkg = app.replace("-", "_")
    srv = (Path(__file__).parents[1] / "src" / pkg / f"{srv_name}.py").read_text()
    real = set(re.findall(r"@mcp\.tool\(\)\s*\ndef\s+(\w+)", srv))
    missing = declared - real
    assert not missing, f"agent frontmatter names tools not in server: {missing}"
