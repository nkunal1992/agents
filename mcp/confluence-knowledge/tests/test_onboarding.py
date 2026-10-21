"""#191: zero-friction onboarding — clipboard seamless path, heartbeat
BEFORE persist, masked confirmation, no token in output."""

from unittest.mock import MagicMock

from confluence_knowledge import cli


def test_mask_never_full():
    assert cli._mask("abcdef1234567890") == "abcd…7890"
    assert "abcdef1234567890" not in cli._mask("abcdef1234567890")


def test_shape_check():
    assert cli._looks_like_token("a" * 30)
    assert not cli._looks_like_token("")
    assert not cli._looks_like_token("short")
    assert not cli._looks_like_token("has spaces in it " * 4)
    assert not cli._looks_like_token("multi\nline\nstuff123456")


def test_noninteractive_requires_transport():
    args = MagicMock(env="cloud", site="https://x.atlassian.net",
                     email="a@b.c", from_clipboard=False,
                     token_stdin=False)
    assert cli._setup_noninteractive(args) == 2


def test_clipboard_garbage_rejected(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_clipboard", lambda: "hello world")
    args = MagicMock(env="cloud", site="https://x.atlassian.net",
                     email="a@b.c", from_clipboard=True,
                     token_stdin=False)
    assert cli._setup_noninteractive(args) == 2
    assert "doesn't look like a token" in capsys.readouterr().err


def test_heartbeat_before_persist(monkeypatch, tmp_path):
    """A token that fails validation must never reach the store (#191)."""
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path / "wd"))
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path / "home"))

    calls = []
    monkeypatch.setattr(cli, "_clipboard", lambda: "tok" + "x" * 30)
    monkeypatch.setattr(cli, "store_credential",
                        lambda *a, **k: calls.append("store") or "keyring")
    monkeypatch.setattr(cli.SiteProfile, "save", lambda *a, **k: None)

    class FakeClient:
        def get(self, ep):
            calls.append("heartbeat")
            r = MagicMock()
            r.json.return_value = {"displayName": "Narayan"}
            return r
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: FakeClient())

    args = MagicMock(env="cloud", site="https://x.atlassian.net",
                     email="a@b.c", from_clipboard=True,
                     token_stdin=False)
    assert cli._setup_noninteractive(args) == 0
    assert calls == ["heartbeat", "store"]   # validate first, persist second


def test_bad_token_persists_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path / "wd"))
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_clipboard", lambda: "tok" + "x" * 30)
    stored = []
    monkeypatch.setattr(cli, "store_credential",
                        lambda *a, **k: stored.append(1) or "keyring")

    def boom(*a, **k):
        raise RuntimeError("401 Unauthorized")
    monkeypatch.setattr(cli, "make_client", boom)
    args = MagicMock(env="cloud", site="https://x.atlassian.net",
                     email="a@b.c", from_clipboard=True,
                     token_stdin=False)
    assert cli._setup_noninteractive(args) == 3
    assert stored == []
    assert "nothing stored" in capsys.readouterr().err


def test_masked_confirm_in_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path / "wd"))
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path / "home"))
    tok = "secrettokenvalue1234"
    monkeypatch.setattr(cli, "_clipboard", lambda: tok)
    monkeypatch.setattr(cli, "store_credential",
                        lambda *a, **k: "os keyring")
    monkeypatch.setattr(cli.SiteProfile, "save", lambda *a, **k: None)
    fc = MagicMock()
    fc.get.return_value.json.return_value = {"displayName": "N"}
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: fc)
    args = MagicMock(env="cloud", site="https://x.atlassian.net",
                     email="a@b.c", from_clipboard=True,
                     token_stdin=False)
    assert cli._setup_noninteractive(args) == 0
    out = capsys.readouterr().out
    assert tok not in out                       # never echo the secret
    assert "secr…1234" in out                   # masked confirm present
    assert "connected as: N" in out


def test_register_clipboard_credential_tool_path(monkeypatch, tmp_path):
    """The MCP-tool twin: same heartbeat→persist ordering, returns data."""
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path / "wd"))
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path / "home"))

    calls = []
    monkeypatch.setattr(cli, "_clipboard", lambda: "tok" + "y" * 30)
    monkeypatch.setattr(cli, "store_credential",
                        lambda *a, **k: calls.append("store") or "keyring")
    monkeypatch.setattr(cli.SiteProfile, "save", lambda *a, **k: None)

    class FakeClient:
        def get(self, ep):
            calls.append("heartbeat")
            r = MagicMock(); r.json.return_value = {"displayName": "Narayan"}
            return r
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: FakeClient())

    out = cli.register_clipboard_credential(
        "cloud", "https://x.atlassian.net", "a@b.c")
    assert out["configured"] is True
    assert out["connected_as"] == "Narayan"
    assert out["token_received"].startswith("toky")
    assert calls == ["heartbeat", "store"]


def test_register_clipboard_credential_empty_clipboard(tmp_path,
                                                     monkeypatch):
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path / "wd"))
    monkeypatch.setattr(cli, "_clipboard", lambda: "   ")
    import pytest
    from confluence_knowledge.settings import SetupError
    with pytest.raises(SetupError, match="clipboard is empty"):
        cli.register_clipboard_credential("cloud", "https://x.atlassian.net",
                                        "a@b.c")


def test_register_token_tool_never_persists_bad_token(tmp_path,
                                                     monkeypatch):
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path / "wd"))
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "_clipboard", lambda: "tok" + "z" * 30)
    stored = []
    monkeypatch.setattr(cli, "store_credential",
                        lambda *a, **k: stored.append(1))

    class Boom:
        def get(self, ep):
            raise RuntimeError("401 Unauthorized")
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: Boom())
    monkeypatch.setattr(cli.SiteProfile, "save", lambda *a, **k: None)

    import pytest
    with pytest.raises(Exception):
        cli.register_clipboard_credential(
            "cloud", "https://x.atlassian.net", "a@b.c")
    assert stored == []
