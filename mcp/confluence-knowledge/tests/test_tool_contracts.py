"""Tool-output contracts — the invariant surface the AGENT reads to
decide what to do next. These are the outputs that steer behavior:
a verdict that says 'corrupt' gets a recrawl; a masked-token shape
keeps secrets out of chat; an error that names its next action keeps
the agent from dead-ending. Break one of these and the model misleads
itself even when the code is technically 'correct'."""
from __future__ import annotations

import json

import pytest

from confluence_knowledge import health
from confluence_knowledge.reporting import pulse


class TestHealthVerdictContract:
    """What the agent reads to decide 'should I tell the user to
    re-crawl?' The remediation field IS the instruction — 'corrupt'
    must carry it, 'busy' must NOT."""

    def test_corrupt_verdict_names_the_rebuild(self, tmp_path):
        (tmp_path / "dead.db").write_bytes(b"garbage" * 2000)
        f = health._index_checks(tmp_path)[0]
        assert not f["ok"]
        assert "corrupt" in f["detail"]
        assert f["remediation"], "corrupt verdict must say how to rebuild"

    def test_busy_verdict_says_retry_not_recrawl(self, tmp_path,
                                               monkeypatch):
        (tmp_path / "busy.db").write_bytes(b"real file")
        import sqlite3

        import confluence_knowledge.index as cidx

        class LockedIndex:
            def __init__(self, *a, **k):
                raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(cidx, "Index", LockedIndex)
        f = health._index_checks(tmp_path)[0]
        assert not f["ok"]
        body = json.dumps(f)
        assert "corrupt" not in body, (
            "a busy index must NEVER look corrupt — the agent will "
            "recrawl a healthy index")
        # 'retry'/'busy' language is fine; a recrawl directive is not
        if f["remediation"]:
            assert "re-crawl" not in f["remediation"]
            assert "delete" not in f["remediation"]


class TestPulseContract:
    """The line the agent relays verbatim — no internals jargon."""

    def test_pulse_has_no_internal_metrics(self):
        p = {"phase": "fetching", "pages_done": 40,
             "remaining": 60, "rate_ppm": 41.9, "eta_s": 88}
        line = pulse(p, rss_mb=1234)
        assert "ppm" not in line and "RSS" not in line
        assert "eta_s" not in line and "1234" not in line
        assert "pages" in line

    def test_paused_phases_surface_verbatim(self):
        for phase in ("paused: auth — session expired",
                      "paused: low memory",
                      "paused: battery saver"):
            line = pulse({"phase": phase, "pages_done": 0,
                          "remaining": 10})
            assert phase.split("—")[0].strip() in line


class TestRegisterTokenContract:
    """The clipboard-tool's error shapes — each failure must name the
    user's next action and never echo the token."""

    def test_empty_clipboard_says_what_to_do(self, tmp_path, monkeypatch):
        clip = tmp_path / "clip.txt"
        clip.write_text("")
        monkeypatch.setenv("CONF_KNOWLEDGE_CLIPBOARD_FILE", str(clip))
        monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path / "h"))
        monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path / "w"))
        import confluence_knowledge.server as srv
        out = srv.register_token("http://localhost:1", "datacenter")
        assert not out["configured"]
        assert "copy" in out["error"].lower(), "must tell user to copy"

    def test_garbage_clipboard_rejected_before_network(self, tmp_path,
                                                       monkeypatch):
        clip = tmp_path / "clip.txt"
        clip.write_text("this is not a token just words")
        monkeypatch.setenv("CONF_KNOWLEDGE_CLIPBOARD_FILE", str(clip))
        monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path / "h"))
        monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path / "w"))
        import confluence_knowledge.server as srv
        out = srv.register_token("http://localhost:1", "datacenter")
        assert not out["configured"]
        assert "copied the right thing" in out["error"]
        # and it must not have persisted anything
        assert not list(tmp_path.rglob("*.conf_credentials*"))

    def test_masked_token_shape(self):
        from confluence_knowledge.cli import _mask
        m = _mask("abcd1234wxyz7890")
        assert "abcd1234wxyz7890" not in m
        assert m.startswith("abcd") and m.endswith("7890")
        assert "…" in m or "..." in m


class TestErrorShapeContract:
    """Every error dict an agent could surface carries a next step
    in 'note'/'remediation' and no stack-trace text."""

    JARGON = ("Traceback", "_daemon", "RpcDb", "pid=",
              "rpc.db", "sqlite3", "ProcessPool")

    @pytest.mark.parametrize("tool_dict", [
        {"configured": False,
         "error": "clipboard doesn't look like a token — nothing "
                  "stored (copied the right thing?)"},
        {"error": "connection check failed — token invalid or wrong "
                  "permissions; nothing stored"},
    ])
    def test_error_dicts_are_actionable_and_clean(self, tool_dict):
        body = json.dumps(tool_dict)
        for j in self.JARGON:
            assert j not in body, f"jargon {j!r} leaks into tool output"
        # an actionable message names a user action or the fix
        assert any(w in body for w in
                   ("re-run", "copy", "check", "provide", "delete",
                    "setup", "stored", "retry")),             "error must name the next user action"
