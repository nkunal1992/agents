"""Unit tests for scripts/pre_release_check.py — check_install is the
defect-catching leg: missing module, missing dist, broken entry point
must each flip the verdict to FAIL."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pre_release_check as prc

PY = sys.executable


def test_check_install_passes_on_healthy_dist():
    """pip itself is always installed in the test venv — a healthy dist
    with importable sentinels must pass end to end."""
    ok, detail = prc.check_install(PY, "pip", modules=["os", "sys"])
    assert ok, detail


def test_check_install_catches_missing_module():
    """The simulated defect — a module the wheel should carry but
    doesn't must flip the verdict."""
    ok, detail = prc.check_install(
        PY, "pip", modules=["definitely_missing_module_zzz"])
    assert not ok
    assert "import" in detail


def test_check_install_catches_missing_dist():
    """pip show on an absent dist is a metadata defect."""
    ok, _ = prc.check_install(PY, "definitely-not-a-real-pkg-zzz",
                              modules=[])
    assert not ok


def test_module_sentinels_cover_all_pkgs():
    assert set(prc.MODULE_SENTINELS) == set(prc.PKGS)
    for pkg, mods in prc.MODULE_SENTINELS.items():
        assert mods, pkg
        prefix = pkg.replace("-", "_")
        assert all(m.startswith(prefix + ".") for m in mods), pkg
