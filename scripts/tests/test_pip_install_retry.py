"""Unit tests for _e2e_common.pip_install — the PyPI index-lag retry (#334)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                    / "mcp" / "confluence-knowledge" / "scripts"))
import _e2e_common as ec


class _P:
    def __init__(self, rc, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_retries_index_miss_then_succeeds(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if len(calls) < 3:
            return _P(1, "ERROR: Could not find a version that satisfies "
                         "the requirement x==9.9.9\nERROR: No matching "
                         "distribution found for x==9.9.9")
        return _P(0, "Successfully installed x-9.9.9")

    monkeypatch.setattr("subprocess.run", fake_run)
    p = ec.pip_install("fake-python", ["x==9.9.9"], {}, "install x",
                       delay_s=0)
    assert p.returncode == 0
    assert len(calls) == 3
    assert calls[0][1:4] == ["-m", "pip", "install"]


def test_no_retry_on_real_error(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _P(1, "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(SystemExit):
        ec.pip_install("fake-python", ["x==9.9.9"], {}, "install x",
                       delay_s=0)
    assert len(calls) == 1


def test_gives_up_after_attempts(monkeypatch):
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return _P(1, "ERROR: No matching distribution found for x")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(SystemExit):
        ec.pip_install("fake-python", ["x==9.9.9"], {}, "install x",
                       attempts=3, delay_s=0)
    assert len(calls) == 3
