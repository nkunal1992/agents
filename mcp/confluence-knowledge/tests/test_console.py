"""#316 — cp1252 consoles must not crash CLI output carrying →/✗."""
import io
import sys

from confluence_knowledge.settings import reconfigure_console


def _cp1252_stream():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252",
                            newline="\n")   # byte-asserts stay platform-agnostic


def test_reconfigure_console_survives_arrow_and_check(monkeypatch):
    out, err = _cp1252_stream(), _cp1252_stream()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    reconfigure_console()
    print("updated 1.0 → 1.1; self-test passed")
    print("  ✗ model_sparse: not found", file=err)
    out.flush(); err.flush()
    assert out.buffer.getvalue() == b"updated 1.0 ? 1.1; self-test passed\n"
    assert err.buffer.getvalue() == b"  ? model_sparse: not found\n"


def test_reconfigure_console_tolerates_plain_streams(monkeypatch):
    monkeypatch.setattr("sys.stdout", io.StringIO())   # no reconfigure
    reconfigure_console()                               # must not raise
    assert sys.stdout.getvalue() == ""
