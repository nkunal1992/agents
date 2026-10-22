"""Unit tests run the direct (non-proxy) path — the daemon is covered
by test_proxy_daemon.py and the IDEKILL stress scenario. Opt back in per
test via monkeypatch.setenv("LOCAL_KNOWLEDGE_DAEMON", "1") (#196)."""

import os

import pytest

os.environ.setdefault("LOCAL_KNOWLEDGE_DAEMON", "0")


@pytest.fixture(autouse=True)
def _isolated_shared_home(monkeypatch, tmp_path):
    """Per-test shared home — no writes into real ~/.knowledge (#206)."""
    monkeypatch.setenv("KNOWLEDGE_SHARED_HOME", str(tmp_path / "shared"))
