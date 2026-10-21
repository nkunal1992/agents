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


def test_agent_frontmatter_tools_exist():
    """Every tool named in the agent's frontmatter must be a real
    @mcp.tool() in the server source."""
    if not AGENT.exists():
        import pytest
        pytest.skip("agent file not in this checkout layout")
    doc = AGENT.read_text()
    tools_line = re.search(r'tools:\s*\[([^\]]+)\]', doc).group(1)
    declared = {t.strip().strip('"').split("/")[1]
                for t in tools_line.split(",")
                if "confluence-knowledge/" in t}
    srv = (Path(__file__).parents[1] / "src" /
           "confluence_knowledge" / "server.py").read_text()
    real = set(re.findall(r"@tool\(mcp[^)]*\)\s*\ndef\s+(\w+)", srv))
    missing = declared - real
    assert not missing, f"agent frontmatter names tools not in server: {missing}"


def test_agent_covers_crawl_terminal_states():
    """`crawl_step` can return `abandoned` (superseded or fenced resume) —
    the agent must instruct a fresh `start_crawl`, not endless polling."""
    doc = AGENT.read_text()
    assert "abandoned" in doc


def test_agent_covers_crawl_awareness():
    """The live wait loop, handoff, cancel, and the non-running states
    must all be instructed — otherwise the agent reverts to blind polls
    and the user never learns anything."""
    doc = AGENT.read_text()
    for token in ("wait_for_crawl", "cancel_crawl", "cancelled",
                  "last_crawl", "notification", "another server process", "the knowledge daemon",
                  "paused: low memory", "pulse", "battery saver"):
        assert token in doc, f"agent missing crawl-awareness token: {token}"
    # user-facing reporting stays plain — internals are for debugging
    assert "Report `pulse`, not internals" in doc


def test_agent_passes_evidence_set_to_verify():
    """`search` returns `evidence_set` and `verify_citations` requires it —
    the agent file must document the plumbing and expiry recovery."""
    doc = AGENT.read_text()
    assert "evidence_set" in doc


def test_agent_verify_contract_is_strict():
    """The strict-quote + entailment contract must be present (R04)."""
    doc = AGENT.read_text()
    assert "non-empty verbatim quote" in doc
    assert "entails" in doc


def test_agent_covers_idle_model_unload():
    """Models unload after ~10 idle min — the agent must treat a `loading`
    note after a pause as normal (#159)."""
    doc = AGENT.read_text()
    assert "idle" in doc and "load" in doc


def test_agent_describes_safe_update():
    """`update` is a safe flow: soaked releases only, self-test after
    install, automatic rollback. The agent must relay that contract plus
    the yanked-release flag."""
    doc = AGENT.read_text()
    assert "soaked" in doc or "soak" in doc, "agent must explain the release soak"
    assert "rolled back" in doc, "agent must explain automatic rollback"
    assert "release" in doc and "yanked" in doc, "agent must cover the release flag"
    assert "selftest" in doc or "self-test" in doc
    assert "-U" not in doc or "never" in doc.lower(), \
        "agent must not prescribe `pip install -U` anymore"


def test_agent_update_targets_subcommand_not_pip():
    """The `update` path goes through the CLI's safe flow — an agent that
    tells users to `pip install -U` bypasses the soak + rollback."""
    doc = AGENT.read_text()
    import re as _re
    assert not _re.search(r"pip install -U", doc), \
        "agent prescribes raw `pip install -U` — must route through update"


def test_onboarding_contract_pinned():
    """The zero-chat onboarding contract must not drift — the agent file
    is what stops 'paste your token in chat' from coming back."""
    doc = AGENT.read_text()
    for token in ("register_token", "clipboard",
                  "configured: false",        # retry loop
                  "reload"):
        assert token in doc, f"agent missing onboarding token: {token}"
    # the privacy invariants stay in writing
    assert "never" in doc.lower() and "chat" in doc.lower()
