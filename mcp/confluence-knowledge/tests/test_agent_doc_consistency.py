"""The agent file must only reference CLI commands that actually exist.

The mcp/confluence-knowledge dir is at ../../../agents/… relative to this file:
repo_root = tests/../..
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
AGENT = REPO / "agents" / "confluence-knowledge.agent.md"


def test_agent_references_only_real_subcommands():
    if not AGENT.exists():
        import pytest
        pytest.skip("agent file not in this checkout layout")
    from confluence_knowledge.cli import main  # noqa: F401 — import only
    # collect the real subcommands from argparse source — cli.main builds them
    src = (Path(__file__).parents[1] / "src" /
           "confluence_knowledge" / "cli.py").read_text()
    real = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', src))
    doc = AGENT.read_text()
    referenced = set(re.findall(r"confluence-knowledge[` ]+([a-z-]+)", doc)) - \
        {"doctor", "-"}
    referenced.discard("exe")  # confluence-knowledge.exe invocations
    for cmd in referenced:
        assert cmd in real, f"agent references `confluence-knowledge {cmd}` — not a real subcommand"


def test_agent_references_repo_files_that_exist():
    doc = AGENT.read_text()
    for m in re.findall(r"prompts/([\w.-]+)", doc):
        assert (REPO / "prompts" / m).exists(), f"agent references missing prompts/{m}"
