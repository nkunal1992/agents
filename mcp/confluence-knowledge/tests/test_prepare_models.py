"""prepare-models must only use real huggingface_hub kwargs."""

import inspect
import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _non_excluded_platform(monkeypatch):
    """These tests exercise the generic prepare path — pin the platform
    predicate off so they behave identically on win_arm64 hosts (#302);
    the excluded-path test overrides it back to True."""
    monkeypatch.setattr(
        "confluence_knowledge.retrieval.sparse_platform_excluded",
        lambda: False)


FAKE_LOCK = {"models": {
    "embed": {"repo": "org/emb", "revision": "r1", "license": "MIT",
              "files": ["m.onnx", "tokenizer.json"]},
    "rerank": {"repo": "org/rer", "revision": "r2", "license": "MIT",
               "files": ["r.onnx", "tokenizer.json"]},
    "rerank_light": {"repo": "org/rl", "revision": "r2b", "license": "MIT",
                     "files": ["r.onnx", "tokenizer.json"]},
    "colbert": {"repo": "org/colb", "revision": "r4", "license": "MIT",
                "files": ["m.onnx", "tokenizer.json"]},
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
    assert len(out) == 5  # embed + 2 rerankers + colbert via snapshot + sparse
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
    assert len(out) == 5


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


def test_prepare_models_skips_sparse_on_excluded_platform(
        monkeypatch, tmp_path):
    """#302: win_arm64 marker-skips fastembed at install — prepare must
    not download a model nothing can load, nor fail on the absent dep."""
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(tmp_path / "models"))
    calls = _fake_hf(monkeypatch, lambda d: (
        (d / "m.onnx").write_bytes(b"x"),
        (d / "r.onnx").write_bytes(b"x"),
        (d / "tokenizer.json").write_text("{}"),
        (d / "LICENSE").write_text("apache")))

    from confluence_knowledge import retrieval
    monkeypatch.setattr(retrieval, "sparse_platform_excluded",
                        lambda: True)
    out = retrieval.prepare_models(tmp_path)
    assert any("skipped" in line and "org/bm25" in line for line in out)
    assert "org/bm25" not in {c[0] for c in calls}  # never instantiated
    assert not (tmp_path / "models" / "fastembed").exists()


def test_missing_license_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(tmp_path / "models"))
    _fake_hf(monkeypatch, lambda d: (
        (d / "m.onnx").write_bytes(b"x"),
        (d / "tokenizer.json").write_text("{}"),
        (d / "README.md").write_text("no license here")))

    from confluence_knowledge import retrieval, settings
    with pytest.raises(settings.SetupError, match="license"):
        retrieval.prepare_models(tmp_path)


# ---- chaos: sha256 integrity enforcement (#211 / audit pillar 1) ----

import hashlib
from copy import deepcopy

LOCK_SHA = deepcopy(FAKE_LOCK)
LOCK_SHA["models"]["embed"]["sha256"] = {
    "m.onnx": hashlib.sha256(b"real-weights").hexdigest()}


def _fake_hf_sha(monkeypatch, good=b"real-weights"):
    calls = []
    fake_hf = types.ModuleType("huggingface_hub")

    def fake_download(repo_id, revision, local_dir, **kw):
        calls.append(repo_id)
        local_dir.mkdir(parents=True, exist_ok=True)
        (local_dir / "m.onnx").write_bytes(good)
        (local_dir / "r.onnx").write_bytes(b"x")
        (local_dir / "tokenizer.json").write_text("{}")
        (local_dir / "LICENSE").write_text("apache")
        return str(local_dir)

    fake_hf.snapshot_download = fake_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)
    monkeypatch.setattr(
        "confluence_knowledge.retrieval._load_lock", lambda: LOCK_SHA)
    fake_fe = types.ModuleType("fastembed")

    class FakeSparse:
        def __init__(self, *a, **kw):
            pass

    fake_fe.SparseTextEmbedding = FakeSparse
    monkeypatch.setitem(sys.modules, "fastembed", fake_fe)
    return calls


def test_corrupt_artifact_refetched_not_stuck(monkeypatch, tmp_path):
    """Tampered file on disk: files_ok must FAIL via hash check → refetch.
    Without this, prepare says 'already prepared' while _require fails —
    the infinite failure loop."""
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(tmp_path / "models"))
    emb = tmp_path / "models" / "emb"
    emb.mkdir(parents=True)
    (emb / "m.onnx").write_bytes(b"corrupted")
    (emb / "tokenizer.json").write_text("{}")
    (emb / ".revision").write_text("r1")

    calls = _fake_hf_sha(monkeypatch)
    from confluence_knowledge import retrieval
    out = retrieval.prepare_models(tmp_path)
    assert any("prepared org/emb" in line for line in out), out
    assert "org/emb" in calls          # refetched the corrupt model
    assert hashlib.sha256(
        (emb / "m.onnx").read_bytes()).hexdigest() == \
        LOCK_SHA["models"]["embed"]["sha256"]["m.onnx"]


def test_still_bad_after_refetch_is_a_hard_fail(monkeypatch, tmp_path):
    """If the refetched bytes STILL mismatch the pinned hash — poisoned
    mirror / broken upstream — refuse loudly instead of running them."""
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(tmp_path / "models"))
    _fake_hf_sha(monkeypatch, good=b"wrong-weights")
    from confluence_knowledge import retrieval, settings
    with pytest.raises(settings.SetupError, match="sha256"):
        retrieval.prepare_models(tmp_path)


def test_require_purges_mismatch_so_prepare_refetches(monkeypatch, tmp_path):
    """_require must DELETE the corrupt file (not just complain) — else
    files_ok stays true and prepare_models reports 'already prepared'
    while every session open fails. The healing loop must exist."""
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(tmp_path / "models"))
    _fake_hf_sha(monkeypatch)
    emb = tmp_path / "models" / "emb"
    emb.mkdir(parents=True)
    (emb / "m.onnx").write_bytes(b"corrupted")
    (emb / "tokenizer.json").write_text("{}")
    (emb / "config.json").write_text("{}")

    from confluence_knowledge import retrieval, settings
    monkeypatch.setattr(retrieval, "EMBED_REPO", "org/emb")
    monkeypatch.setattr(retrieval, "EMBED_ONNX", "m.onnx")
    with pytest.raises(settings.SetupError, match="sha256"):
        retrieval._require(emb, "org/emb", "cli", ["m.onnx"])
    assert not (emb / "m.onnx").exists(), "corrupt file must be purged"
    # and now a fresh prepare refetches it — no infinite loop
    out = retrieval.prepare_models(tmp_path)
    assert any("prepared org/emb" in line for line in out)
