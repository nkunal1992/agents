"""health_checks flags: each failure mode → exactly one remediation."""


from pathlib import Path

import pytest

from confluence_knowledge.health import health_checks
from confluence_knowledge.index import Index
from confluence_knowledge.retrieval import MODEL_MARKER
from confluence_knowledge.settings import SetupError


def _flag(checks, name):
    return {c["flag"]: c for c in checks}[name]


def test_models_complete_flags_partial_download(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(home / "models"))
    # partial embed model: dir exists, onnx missing — the old shallow
    # check reported this as "ready"
    (home / "models" / "nomic-embed-text-v1.5") \
        .mkdir(parents=True)
    (home / "models" / "nomic-embed-text-v1.5"
        / "tokenizer.json").write_text("{}")
    checks = health_checks(home, tmp_path / "work", "sid", "ck")
    emb = _flag(checks, "model_embed")
    assert emb["ok"] is False and "prepare-models" in emb["remediation"]


def test_models_complete_green_when_full(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(home / "models"))
    for name, files in (
            ("snowflake-arctic-embed-xs",
             ["onnx/model_quantized.onnx",
              "tokenizer.json", "tokenizer_config.json",
              "config.json", "special_tokens_map.json",
              "README.md"]),
            ("mxbai-rerank-xsmall-v1", ["onnx/model_quantized.onnx",
                                        "tokenizer.json",
                                        "tokenizer_config.json",
                                        "config.json",
                                        "special_tokens_map.json",
                                        "onnx/quantize_config.json",
                                        "README.md"]),
            ("mxbai-rerank-base-v1", ["onnx/model_quantized.onnx",
                                      "tokenizer.json",
                                      "tokenizer_config.json",
                                      "config.json",
                                      "special_tokens_map.json",
                                      "onnx/quantize_config.json",
                                      "README.md"])):
        for f in files:
            p = home / "models" / name / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
    (home / "models" / "fastembed" / "x").mkdir(parents=True)
    checks = health_checks(home, tmp_path / "work", "sid", "ck")
    for f in ("model_embed", "model_rerank", "model_rerank_light",
              "model_sparse"):
        assert _flag(checks, f)["ok"] is True, f


def test_sparse_platform_excluded_degrades_checks(tmp_path, monkeypatch):
    """#302: on win_arm64 the install marker skips fastembed — doctor
    must show the sparse leg as platform-degraded, not missing/dep-broken,
    or remediation tells the user to reinstall what can't exist."""
    import importlib.util

    from confluence_knowledge import health, retrieval

    monkeypatch.setattr(retrieval, "sparse_platform_excluded",
                        lambda: True)
    real_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda m: None if m == "fastembed" else real_spec(m))

    home = tmp_path / "home"
    monkeypatch.setenv("KNOWLEDGE_MODELS_DIR", str(home / "models"))
    checks = health.health_checks(home, tmp_path / "work", "sid", "ck")
    # fastembed must not be a required dep on the excluded platform —
    # other deps may legitimately be absent in this env, so assert the
    # missing-list contents, not the aggregate ok
    deps = _flag(checks, "deps")
    assert "fastembed" not in deps["detail"]
    sp = _flag(checks, "model_sparse")
    assert sp["ok"] is True
    assert sp["detail"] == "platform-degraded"
    assert sp["remediation"] is None


def test_index_corrupt_flagged(tmp_path):
    idx_root = tmp_path / "idx"
    idx_root.mkdir()
    (idx_root / "scope1.db").write_bytes(b"not a sqlite file" * 100)
    checks = health_checks(tmp_path / "home", tmp_path / "work", "sid", "ck",
                           index_root=idx_root)
    f = [c for c in checks if c["flag"].startswith("index:")]
    assert f and f[0]["ok"] is False and "corrupt" in f[0]["detail"]


def test_index_stale_model_flagged(tmp_path):
    idx_root = tmp_path / "idx"
    idx_root.mkdir()
    idx = Index(idx_root / "scope1.db")
    idx.set_model_id("old-model-xyz")
    checks = health_checks(tmp_path / "home", tmp_path / "work", "sid", "ck",
                           index_root=idx_root)
    f = [c for c in checks if c["flag"].startswith("index:")]
    assert f and f[0]["ok"] is False and "start_crawl" in f[0]["remediation"]


def test_index_healthy_passes(tmp_path):
    idx_root = tmp_path / "idx"
    idx_root.mkdir()
    idx = Index(idx_root / "scope1.db")
    idx.set_model_id(MODEL_MARKER)
    checks = health_checks(tmp_path / "home", tmp_path / "work", "sid", "ck",
                           index_root=idx_root)
    f = [c for c in checks if c["flag"].startswith("index:")]
    assert f and f[0]["ok"] is True


def test_corrupt_db_raises_setup_error_not_sqlite(tmp_path):
    db = tmp_path / "bad.db"
    db.write_bytes(b"garbage" * 1000)
    with pytest.raises(SetupError, match="index unreadable"):
        Index(db)


def test_registration_missing_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr("confluence_knowledge.registration.targets",
                        lambda ide, workspace=None: [])
    checks = health_checks(tmp_path / "home", tmp_path / "work", "sid", "ck")
    reg = _flag(checks, "registration")
    assert reg["ok"] is False and "register" in reg["remediation"]


def test_deps_flag_when_missing(tmp_path, monkeypatch):
    """A genuinely missing dep turns the flag red — tested on the
    non-excluded path (where fastembed IS expected); #302's degrade is
    covered by test_sparse_platform_excluded_degrades_checks."""
    import importlib.util

    from confluence_knowledge import retrieval
    monkeypatch.setattr(retrieval, "sparse_platform_excluded",
                        lambda: False)
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda m: None if m == "fastembed" else real(m))
    checks = health_checks(tmp_path / "home", tmp_path / "work", "sid", "ck")
    assert _flag(checks, "deps")["ok"] is False


def test_every_red_flag_has_a_remediation(tmp_path, monkeypatch):
    """The agent's contract: a red flag never lands without a fix command."""
    monkeypatch.setattr("confluence_knowledge.registration.targets",
                        lambda ide, workspace=None: [])
    checks = health_checks(tmp_path / "home", tmp_path / "work", "sid", "ck")
    red = [c for c in checks if c["ok"] is False and c["flag"] != "proxy"]
    assert red  # some flags SHOULD be red on a bare tmp home
    for c in red:
        assert c["remediation"], f"red flag {c['flag']} has no remediation"


def test_documented_flags_exist(tmp_path, monkeypatch):
    """Every flag named in the agent's remediation table must be a flag
    doctor can actually emit — drift silently strands users (hunt F)."""
    import re
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path))
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path / "w"))
    idx_root = tmp_path / "idx"
    idx_root.mkdir()
    Index(idx_root / "scope.db")  # so index:* flags are emitted
    checks = health_checks(tmp_path / "home", tmp_path / "w",
                           "confluence-knowledge", "confluence-knowledge",
                           index_root=idx_root)
    emitted = {c["flag"].split(":")[0] for c in checks}
    emitted.add("creds_valid")  # network flag — status() level, not here
    agent = (Path(__file__).parents[3] / "agents"
             / "confluence-knowledge.agent.md").read_text()
    documented = set(re.findall(r"`(deps|model_\w+|registration|index|"
                              r"disk_free|python|proxy|creds_valid)", agent))
    missing = documented - emitted
    assert not missing, f"agent documents flags doctor never emits: {missing}"


def test_index_unreadable_is_not_corrupt(tmp_path, monkeypatch):
    """Field case: a transiently-locked index (WAL write / Windows file
    lock) must NOT report 'corrupt' — that verdict sent a user recrawling
    a healthy index. Unreadable → 'couldn't read' + no remediation."""
    from confluence_knowledge.health import _index_checks
    from confluence_knowledge.index import Index
    idx_root = tmp_path / "idx"; idx_root.mkdir()
    Index(idx_root / "scope.db").close()
    # force the open to fail — the transient path
    import confluence_knowledge.index as idxmod
    def boom(*a, **k):
        raise OSError("database is locked")
    monkeypatch.setattr(idxmod.Index, "__init__", boom)
    flags = _index_checks(idx_root)
    assert flags and "corrupt" not in flags[0]["detail"], flags
    assert "couldn't read" in flags[0]["detail"]
    assert flags[0]["remediation"] is None   # no recrawl advice
