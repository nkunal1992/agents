"""prepare-models must only use real huggingface_hub kwargs."""

import inspect
import sys
import types

import pytest

FAKE_LOCK = {"models": {
    "embed": {"repo": "org/emb", "revision": "r1", "license": "MIT",
              "files": ["m.onnx", "tokenizer.json"]},
    "rerank": {"repo": "org/rer", "revision": "r2", "license": "MIT",
               "files": ["r.onnx", "tokenizer.json"]},
    "rerank_light": {"repo": "org/rl", "revision": "r2b", "license": "MIT",
                     "files": ["r.onnx", "tokenizer.json"]},
    "sparse": {"repo": "org/bm25", "revision": "r3", "license": "Apache-2.0"},
}}


def _fake_hf(monkeypatch, write):
    calls = []

    fake_hf = types.ModuleType("huggingface_hub")

    def fake_download(repo_id, revision, local_dir, **kw):
        calls.append((repo_id, kw))
        local_dir.mkdir(parents=True, exist_ok=True)
        write(local_dir)
        return str(local_dir)

    fake_hf.snapshot_download = fake_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)
    monkeypatch.setattr(
        "confluence_knowledge.retrieval._load_lock", lambda: FAKE_LOCK)
    # fastembed owns the sparse download — fake the class it instantiates
    fake_fe = types.ModuleType("fastembed")

    class FakeSparse:
        def __init__(self, *a, **kw):
            calls.append((a[0] if a else None, kw))

    fake_fe.SparseTextEmbedding = FakeSparse
    monkeypatch.setitem(sys.modules, "fastembed", fake_fe)
    return calls


def test_snapshot_download_callable_kwargs():
    """Guard the exact bug we hit: kwargs must exist in the real signature."""
    hf = pytest.importorskip("huggingface_hub")
    params = set(inspect.signature(hf.snapshot_download).parameters)
    for k in ("repo_id", "revision", "local_dir", "allow_patterns",
              "ignore_patterns"):
        assert k in params, f"snapshot_download no longer accepts {k}"


def test_prepare_models_downloads_locked_files(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(tmp_path / "models"))
    # a seeded shared store on the dev machine must not hijack the test's
    # model root — the env seam is the designed override (#206)
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(tmp_path / "models"))
    calls = _fake_hf(monkeypatch, lambda d: (
        (d / "m.onnx").write_bytes(b"x"),
        (d / "r.onnx").write_bytes(b"x"),
        (d / "tokenizer.json").write_text("{}"),
        (d / "LICENSE").write_text("apache")))

    from confluence_knowledge import retrieval
    out = retrieval.prepare_models(tmp_path)
    assert len(out) == 4  # embed + 2 rerankers via snapshot + sparse
    repos = {c[0] for c in calls}
    assert "org/emb" in repos and "org/rer" in repos and "org/bm25" in repos
    for _, kw in calls:
        assert "exclude_patterns" not in kw


def test_model_card_license_accepted(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(tmp_path / "models"))
    _fake_hf(monkeypatch, lambda d: (
        (d / "m.onnx").write_bytes(b"x"),
        (d / "r.onnx").write_bytes(b"x"),
        (d / "tokenizer.json").write_text("{}"),
        (d / "README.md").write_text(
            "---\nlanguage: en\nlicense: mit\n---\nmodel card")))

    from confluence_knowledge import retrieval
    out = retrieval.prepare_models(tmp_path)
    assert len(out) == 4


def test_partial_download_is_retried(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(tmp_path / "models"))
    calls = _fake_hf(monkeypatch, lambda d: (
        (d / "m.onnx").write_bytes(b"x"),
        (d / "r.onnx").write_bytes(b"x"),
        (d / "tokenizer.json").write_text("{}"),
        (d / "LICENSE").write_text("apache")))

    # simulate two aborted runs: dirs exist but lack the locked files
    for name in ("emb", "rer"):
        (tmp_path / "models" / name).mkdir(parents=True)

    from confluence_knowledge import retrieval
    out = retrieval.prepare_models(tmp_path)
    assert all("prepared" in line for line in out)
    assert len([c for c in calls if c[0] in ("org/emb", "org/rer")]) == 2

    out2 = retrieval.prepare_models(tmp_path)
    assert all(("already prepared" in line or "org/bm25" in line)
               for line in out2)


def test_missing_license_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(tmp_path / "models"))
    _fake_hf(monkeypatch, lambda d: (
        (d / "m.onnx").write_bytes(b"x"),
        (d / "tokenizer.json").write_text("{}"),
        (d / "README.md").write_text("no license here")))

    from confluence_knowledge import retrieval, settings
    with pytest.raises(settings.SetupError, match="license"):
        retrieval.prepare_models(tmp_path)
