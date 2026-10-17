"""Self-update check — mocked index, offline path, version compare."""
from __future__ import annotations

from confluence_knowledge import update


def test_installed_version():
    v = update.installed_version("confluence-knowledge")
    assert v and v[0].isdigit()


def test_latest_offline_returns_none(monkeypatch):
    monkeypatch.setattr(update, "INDEX", "https://invalid.invalid/{dist}")
    monkeypatch.delenv("KNOWLEDGE_UPDATE_INDEX", raising=False)
    # urllib will fail to resolve — must return None, not raise
    assert update.latest_version("confluence-knowledge", timeout=1) is None


def test_check_update_available(monkeypatch):
    monkeypatch.setattr(update, "latest_version", lambda d, **k: "99.0.0")
    r = update.check("confluence-knowledge")
    assert r["update_available"] is True and r["latest"] == "99.0.0"


def test_check_up_to_date(monkeypatch):
    inst = update.installed_version("confluence-knowledge")
    monkeypatch.setattr(update, "latest_version", lambda d, **k: inst)
    r = update.check("confluence-knowledge")
    assert r["update_available"] is False


def test_check_cannot_check(monkeypatch):
    monkeypatch.setattr(update, "latest_version", lambda d, **k: None)
    r = update.check("confluence-knowledge")
    assert r["update_available"] is False and r["latest"] is None


def test_version_tuple():
    assert update._version_tuple("1.2.10") > update._version_tuple("1.2.9")
    assert update._version_tuple("0.4.1") == update._version_tuple("0.4.1")
