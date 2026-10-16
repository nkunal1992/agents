"""prepare-models must only use real huggingface_hub kwargs."""

import inspect
import sys
import types

import pytest


def test_snapshot_download_callable_kwargs():
    """Guard the exact bug we hit: kwargs must exist in the real signature."""
    hf = pytest.importorskip("huggingface_hub")
    params = set(inspect.signature(hf.snapshot_download).parameters)
    for k in ("repo_id", "revision", "local_dir", "allow_patterns",
              "ignore_patterns"):
        assert k in params, f"snapshot_download no longer accepts {k}"


def test_prepare_models_uses_ignore_patterns(monkeypatch, tmp_path):
    """Fake hub module: assert we never pass exclude_patterns."""
    calls = []

    fake_hf = types.ModuleType("huggingface_hub")

    def fake_download(repo_id, revision, local_dir, **kw):
        calls.append(kw)
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "LICENSE").write_text("apache")
        return str(local_dir)

    fake_hf.snapshot_download = fake_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)
    monkeypatch.setattr(
        "confluence_knowledge.retrieval._load_lock",
        lambda: {"models": {"embed": {"revision": "r1"},
                            "rerank": {"revision": "r2"}}})

    from confluence_knowledge import retrieval
    out = retrieval.prepare_models(tmp_path)
    assert calls, "snapshot_download never called"
    assert len(out) == 2
    for kw in calls:
        assert "exclude_patterns" not in kw
