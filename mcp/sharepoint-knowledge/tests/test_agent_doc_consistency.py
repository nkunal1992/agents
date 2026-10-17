"""The sharepoint-knowledge agent file must only reference real subcommands."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
AGENT = REPO / "agents" / "sharepoint-knowledge.agent.md"


def test_agent_references_only_real_subcommands():
    if not AGENT.exists():
        import pytest
        pytest.skip("agent file not in this checkout layout")
    src = (Path(__file__).parents[1] / "src" /
           "sharepoint_knowledge" / "cli_sp.py").read_text()
    real = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', src))
    doc = AGENT.read_text()
    referenced = set(re.findall(r"sharepoint-knowledge[` ]+([a-z-]+)", doc)) - \
        {"doctor", "-", "exe", "service", "agent", "knowledge", "bundle"}
    for cmd in referenced:
        assert cmd in real, \
            f"agent references `sharepoint-knowledge {cmd}` — not a real subcommand"


def test_agent_references_repo_files_that_exist():
    doc = AGENT.read_text()
    for m in re.findall(r"prompts/([\w.-]+)", doc):
        assert (REPO / "prompts" / m).exists(), \
            f"agent references missing prompts/{m}"
