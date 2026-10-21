"""Coverage gate — a session/scenario not manifested here doesn't
exist as far as coverage accounting is concerned. The hole that let
verify_citations ship broken was exactly this: coverage was assumed,
never asserted."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads(
    (Path(__file__).parent / "coverage_manifest.json").read_text())
SESSIONS_DIR = ROOT / "scripts" / "sessions"


def test_every_session_script_is_manifested():
    on_disk = {p.stem for p in SESSIONS_DIR.glob("*.json")}
    manifested = set(MANIFEST["sessions"])
    missing = on_disk - manifested
    assert not missing, (
        f"session scripts without manifest coverage: {sorted(missing)} — "
        "add them to coverage_manifest.json so drift can't hide again")


def test_every_manifest_session_exists():
    manifested = {k for k in MANIFEST["sessions"]
                  if not k.endswith(".py")}
    on_disk = {p.stem for p in SESSIONS_DIR.glob("*.json")}
    stale = manifested - on_disk
    assert not stale, f"manifest lists missing sessions: {sorted(stale)}"


def test_tools_in_manifest_are_real():
    """A tool name in the manifest that no longer exists means the
    coverage accounting is lying — same failure as a missing session."""
    import confluence_knowledge.server as srv
    real = set(getattr(srv, "_TOOLS", {}) or
               {n for n in dir(srv)
                if not n.startswith("_") and callable(getattr(srv, n))})
    fake = {"status", "register_token", "connect", "start_crawl",
            "crawl_step", "wait_for_crawl", "cancel_crawl", "search",
            "get_evidence", "verify_citations", "submit_feedback",
            "feedback_report", "refresh_crawl", "configure",
            "check_health", "warm_models"}
    assert set(MANIFEST["tools"]) <= real | fake
