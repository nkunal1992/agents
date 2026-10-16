"""Secrets: gitignore-before-write, permissions, canary non-leakage."""

import os
import stat

from confluence_knowledge.auth import redact, store_credential
from confluence_knowledge.settings import AUTH_FILE_NAME


def test_gitignore_written_before_secret(tmp_path, monkeypatch):
    from confluence_knowledge import auth
    monkeypatch.setattr(auth, "_keyring_store", lambda r, s: False)
    where = store_credential(tmp_path, tmp_path / "home", "ref1", "tok_ABC123")
    gi = (tmp_path / ".gitignore").read_text()
    assert AUTH_FILE_NAME in gi and ".conf_knowledge_index/" in gi
    assert where  # file fallback path recorded


def test_secret_file_permissions(tmp_path, monkeypatch):
    # force file fallback: make keyring unusable
    from confluence_knowledge import auth
    monkeypatch.setattr(auth, "_keyring_store", lambda r, s: False)
    store_credential(tmp_path, tmp_path / "home", "ref1", "tok_ABC123")
    if os.name != "nt":
        mode = (tmp_path / AUTH_FILE_NAME).stat().st_mode
        assert stat.S_IMODE(mode) == 0o600


def test_canary_redaction():
    out = redact("auth failed for header Bearer tok_ABC123 retry", ["tok_ABC123"])
    assert "tok_ABC123" not in out and "***" in out
