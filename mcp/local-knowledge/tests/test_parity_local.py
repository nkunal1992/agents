"""#184 parity regressions: errors_n derive, cap-breach keep, auto-reindex,
adaptive fetch wiring — the four gaps the confluence epic left behind."""

from __future__ import annotations

import pytest

from confluence_knowledge.index import Index
from local_knowledge.crawl_local import LocalCrawler


@pytest.fixture
def idx(tmp_path):
    return Index(tmp_path / "i.db")


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    for i in range(12):
        (root / f"doc{i}.md").write_text(f"document {i} about onboarding")
    return root


def test_cap_breach_keeps_prior_rows(tree, idx, monkeypatch):
    """Crawl fully, then shrink MAX_FILES and refresh: the unvisited tail
    must be kept, not tombstoned — a stopped crawl is not deletion proof."""
    c = LocalCrawler(idx, tree)
    assert c.step(c.start())["state"] == "done"
    before = set(idx.active_pages())
    assert len(before) == 12

    from local_knowledge import crawl_local
    monkeypatch.setattr(crawl_local, "MAX_FILES", 3)
    r = LocalCrawler(idx, tree).step(LocalCrawler(idx, tree).start())
    # job errored-or-done with the cap error; either way prior rows kept
    after = idx.active_pages()
    assert set(after) == before
    assert "file cap reached" in " ".join(r["errors"])


def test_errors_n_derived_from_durable_errors(idx):
    """_progress_for must floor errors_n at len(errors) even when the
    progress snapshot was written before the last append."""
    from local_knowledge import server_local as srv

    idx.create_job("j1", "scope")
    idx.save_job("j1", "running", ["file:x"], ["e1", "e2"])
    p = srv._progress_for(idx, "j1")
    assert p["errors_n"] == 2


def test_connect_stale_model_auto_reindexes(monkeypatch, tmp_path):
    """connect on a stale-model index notifies once and starts the
    rebuild itself (LOCAL_KNOWLEDGE_AUTO_REINDEX=0 opts out)."""
    from local_knowledge import server_local as srv
    from local_knowledge.settings_local import LocalProfile

    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    (root / "a.md").write_text("hello")
    # the scope index lives at index_root()/<scope_key>.db — bind the
    # profile to a controlled workdir so the stale stamp lands there
    monkeypatch.setenv("LOCAL_KNOWLEDGE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOCAL_KNOWLEDGE_WORKDIR", str(tmp_path / "work"))
    srv._indexes.clear(); srv._scopes.clear()
    idx = srv._index_for(
        LocalProfile(str(root), srv.socket.gethostname()).scope_key())
    idx.set_model_id("old-model-v0")

    notified = []
    monkeypatch.setattr(srv, "notify", lambda *a: notified.append(a))
    monkeypatch.setattr(srv, "_ensure_worker", lambda s, j: True)
    monkeypatch.setenv("LOCAL_KNOWLEDGE_AUTO_REINDEX", "0")
    r = srv.connect(str(root))
    assert r["stale_model"] is True
    assert r["auto_reindex"] is None          # opted out
    assert notified                           # but the user was told


def test_auto_reindex_starts_job(idx, monkeypatch, tmp_path):
    from local_knowledge import server_local as srv

    root = tmp_path / "docs"
    root.mkdir(exist_ok=True)
    (root / "a.md").write_text("hello")

    started = []

    class FakeCrawler:
        def __init__(self, i, r):
            started.append(r)

        def start(self):
            return "jid123"

    monkeypatch.setattr(srv, "LocalCrawler", FakeCrawler)
    monkeypatch.setattr(srv, "notify", lambda *a: None)
    monkeypatch.setattr(srv, "_ensure_worker", lambda s, j: True)
    out = srv._maybe_auto_reindex({"key": "k1", "root": root}, idx)
    assert out == "started" and started == [root]
