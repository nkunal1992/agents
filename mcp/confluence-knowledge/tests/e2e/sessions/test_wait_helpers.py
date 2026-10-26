"""#239 — unit coverage for the respawn-poll helpers. No pytestmark:
these spawn nothing, so they run in the regular unit leg."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import _wait  # noqa: E402


def test_respawn_deadline_default_and_override(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_E2E_RESPAWN_S", raising=False)
    assert _wait.respawn_deadline() == 120.0
    monkeypatch.setenv("KNOWLEDGE_E2E_RESPAWN_S", "300")
    assert _wait.respawn_deadline() == 300.0


def test_supervision_tail_reads_both_logs(tmp_path):
    (tmp_path / "daemon.log").write_text("d1\nd2\n")
    (tmp_path / "watchdog.log").write_text("w1\n")
    out = _wait.supervision_tail(tmp_path)
    assert "d2" in out and "w1" in out
    assert "daemon.log" in out and "watchdog.log" in out


def test_supervision_tail_missing_logs(tmp_path):
    assert "no daemon.log" in _wait.supervision_tail(tmp_path)


def test_watchdog_failure_no_pid_file(tmp_path):
    assert _wait.watchdog_failure(tmp_path) is None


def test_watchdog_failure_dead_pid(tmp_path, monkeypatch):
    (tmp_path / ".watchdog.pid").write_text("424242")
    monkeypatch.setattr("confluence_knowledge.rpc._cmdline_is",
                        lambda pid, frag: False)
    assert "dead" in _wait.watchdog_failure(tmp_path)


def test_watchdog_failure_alive_watchdog(tmp_path, monkeypatch):
    (tmp_path / ".watchdog.pid").write_text("1234")
    monkeypatch.setattr("confluence_knowledge.rpc._cmdline_is",
                        lambda pid, frag: True)
    assert _wait.watchdog_failure(tmp_path) is None


def test_watchdog_failure_mid_write_is_open(tmp_path, monkeypatch):
    """An unparseable pid file is a rewrite-in-progress — stay open,
    never false-trigger the early fail."""
    (tmp_path / ".watchdog.pid").write_text("")
    monkeypatch.setattr("confluence_knowledge.rpc._cmdline_is",
                        lambda pid, frag: False)
    assert _wait.watchdog_failure(tmp_path) is None
