"""LocalCrawler: file/folder scope, secret exclusion, symlink guards, resume."""
from __future__ import annotations

import json

import pytest

from confluence_knowledge.index import Index
from confluence_knowledge.settings import SetupError
from local_knowledge.crawl_local import LocalCrawler, resolve_root


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "guide.md").write_text("the gateway restart procedure")
    (root / "sub" / "deep.txt").write_text("nested content here")
    (root / "data.csv").write_text("k,v\na,1\n")
    (root / ".env").write_text("SECRET=x")
    (root / "id_rsa").write_text("PRIVATE KEY")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("should be skipped")
    (root / "weird.bin").write_bytes(b"\x00\x01\x02")
    return root


@pytest.fixture
def idx(tmp_path):
    return Index(tmp_path / "i.db")


def run_all(c: LocalCrawler, job: str) -> dict:
    r = c.step(job)
    return r


def test_crawl_folder(tree, idx):
    c = LocalCrawler(idx, tree)
    r = run_all(c, c.start())
    assert r["state"] == "done"
    pages = idx.active_pages()
    titles = {p["title"] for p in pages.values()}
    assert {"guide.md", "deep.txt", "data.csv"} <= titles
    # secret files + .git contents never indexed
    assert "SECRET=x" not in " ".join(p["title"] for p in pages.values())
    assert ".env" not in titles and "id_rsa" not in titles
    assert "config" not in titles
    # excluded + non-indexable disclosed — secrets + the .git dir skip land
    # in job warnings (not coverage-degrading errors), content issues in
    # that page's warnings (#281)
    assert "secret" in " ".join(r["warnings"])
    assert "noise/cache dir" in " ".join(r["warnings"])
    warn_pages = [w for p in idx.db.execute(
        "SELECT warnings FROM pages").fetchall() for w in json.loads(p[0])]
    assert any("binary" in w for w in warn_pages)


def test_crawl_single_file(tmp_path, idx):
    f = tmp_path / "one.txt"
    f.write_text("solo file content")
    c = LocalCrawler(idx, f)
    r = run_all(c, c.start())
    assert r["state"] == "done" and r["pages_seen"] == 1
    assert idx.active_chunks()


def test_secret_file_directly_is_indexed(tmp_path, idx):
    """An explicit single-file root is indexed even if it looks like a secret
    — the user pointed at it directly; disclosure only applies to listings."""
    f = tmp_path / ".env"
    f.write_text("USER_SECRET=abc")
    c = LocalCrawler(idx, f)
    r = run_all(c, c.start())
    assert r["pages_seen"] == 1  # indexed — explicit user choice


def test_symlink_escape_disclosed(tmp_path, idx):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("inside")
    outside = tmp_path / "outside.txt"
    outside.write_text("escaped content")
    link = root / "escape.txt"
    link.symlink_to(outside)
    c = LocalCrawler(idx, root)
    r = run_all(c, c.start())
    assert "escape" in " ".join(r["warnings"])
    titles = {p["title"] for p in idx.active_pages().values()}
    assert "escape.txt" not in titles and "a.txt" in titles


def test_dir_cycle_terminates(tmp_path, idx):
    root = tmp_path / "docs"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("x")
    (root / "sub" / "loop").symlink_to(root, target_is_directory=True)
    c = LocalCrawler(idx, root)
    r = run_all(c, c.start())
    assert r["state"] == "done"  # terminates — no infinite queue


def test_dotgithub_indexes_and_skip_dir_disclosed(tmp_path, idx):
    """#281: a '.git*' prefix check silently dropped '.github' trees; noise
    dirs are still skipped but now every skip is disclosed in warnings."""
    root = tmp_path / "docs"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "build").mkdir()
    (root / ".github" / "workflows" / "ci.txt").write_text("ci steps here")
    (root / "build" / "artifact.md").write_text("build notes")
    c = LocalCrawler(idx, root)
    r = run_all(c, c.start())
    titles = {p["title"] for p in idx.active_pages().values()}
    assert "ci.txt" in titles              # .github indexes now
    assert "artifact.md" not in titles     # build still skipped…
    w = " ".join(r["warnings"])
    assert "build" in w and "noise/cache dir" in w   # …but disclosed


def test_non_regular_file_disclosed(tmp_path, idx):
    """#281: fifo/socket/dangling-symlink entries silently vanished."""
    import os
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("x")
    try:
        os.mkfifo(root / "pipe")
    except (AttributeError, OSError):
        pytest.skip("no mkfifo on this platform")
    c = LocalCrawler(idx, root)
    r = run_all(c, c.start())
    assert "not a regular file" in " ".join(r["warnings"])


def test_text_like_files_index_content(tmp_path, idx):
    """#281: logs/configs/source/extensionless docs were 'indexed' with
    zero chunks — unsearchable. Auto-detection makes them searchable."""
    root = tmp_path / "docs"
    (root / "data").mkdir(parents=True)
    (root / "data" / "app.log").write_text("gateway restart procedure")
    (root / "data" / "conf.yaml").write_text("gateway: restart-first")
    (root / "README").write_text("the onboarding runbook lives here")
    c = LocalCrawler(idx, root)
    r = run_all(c, c.start())
    assert r["state"] == "done"
    pages = idx.active_pages()
    assert {"app.log", "conf.yaml", "README"} <= \
        {p["title"] for p in pages.values()}
    # every page must carry real chunks — no searchable-empty pages
    for pid, p in pages.items():
        n = idx.db.execute(
            "SELECT COUNT(*) FROM chunks WHERE page_id=? AND active=1",
            (pid,)).fetchone()[0]
        assert n > 0, f"{p['title']} indexed with zero chunks"


def test_checkpoint_retains_pending_files(tmp_path, idx, monkeypatch):
    """#281: files extracted but still buffered in `pending` were missing
    from the durable queue — a crash before their flush dropped them, and
    the resumed crawl tombstoned their pages. 60 one-chunk files keep
    pending non-empty when the 50-item checkpoint fires."""
    import numpy as np
    root = tmp_path / "docs"
    root.mkdir()
    for i in range(60):
        (root / f"f{i:02}.txt").write_text(f"unique content {i} " * 20)

    captured: list[list[str]] = []
    orig = LocalCrawler._checkpoint

    def spy(self, job_id, queue, *a, **k):
        captured.append(list(queue))
        return orig(self, job_id, queue, *a, **k)

    monkeypatch.setattr(LocalCrawler, "_checkpoint", spy)

    class Emb:
        def embed(self, texts, query: bool = False):
            return np.ones((len(texts), 4), dtype=np.float32)

    c = LocalCrawler(idx, root, embedder=Emb())
    r = run_all(c, c.start())
    assert r["state"] == "done"
    assert captured, "expected at least one throttled checkpoint"
    # the in-loop checkpoint (first capture) must carry the pending files —
    # pre-fix it held only the unprocessed tail (~10 entries)
    first = [e for e in captured[0] if e.startswith("file:")]
    assert len(first) >= 45, \
        f"checkpoint dropped pending files: only {len(first)} file entries"
    assert len(idx.active_pages()) == 60


def test_max_files_env_override(monkeypatch):
    """#281: the 20k file cap is env-tunable — garbage must not break the
    module import, and the floor clamps at 100."""
    import importlib

    import local_knowledge.crawl_local as cl
    for raw, want in (("500", 500), ("5", 100), ("bogus", 20_000)):
        monkeypatch.setenv("LOCAL_KNOWLEDGE_MAX_FILES", raw)
        importlib.reload(cl)
        assert cl.MAX_FILES == want, f"{raw} → {cl.MAX_FILES}"
    monkeypatch.delenv("LOCAL_KNOWLEDGE_MAX_FILES", raising=False)
    importlib.reload(cl)


def test_resolve_root(tmp_path):
    assert resolve_root(str(tmp_path)) == tmp_path.resolve()
    with pytest.raises(SetupError):
        resolve_root(str(tmp_path / "nope"))


def test_publish_is_atomic(tree, idx, tmp_path):
    """A crawl that later sees an empty folder publishes only the new set."""
    c = LocalCrawler(idx, tree)
    run_all(c, c.start())
    assert idx.stats()["pages"] >= 3
    # delete a file and re-crawl — the index drops it from active
    (tree / "sub" / "deep.txt").unlink()
    c2 = LocalCrawler(idx, tree)
    r2 = run_all(c2, c2.start())
    assert r2["state"] == "done"
    titles = {p["title"] for p in idx.active_pages().values()}
    assert "deep.txt" not in titles


def test_unchanged_files_skip_extract_and_embed(tree, idx):
    """Second crawl over identical bytes: hash match → copy live rows —
    no re-extract, no re-embed."""
    import numpy as np

    class Emb:
        def __init__(self):
            self.n = 0

        def embed(self, texts, query: bool = False):
            self.n += len(texts)
            return np.ones((len(texts), 4), dtype=np.float32)

    emb = Emb()
    c = LocalCrawler(idx, tree, embedder=emb)
    r = run_all(c, c.start())
    assert r["state"] == "done" and emb.n > 0
    seen_first = idx.stats()["pages"]

    emb2 = Emb()
    c2 = LocalCrawler(idx, tree, embedder=emb2)
    r2 = run_all(c2, c2.start())
    assert r2["state"] == "done"
    assert emb2.n == 0                        # nothing re-embedded
    assert idx.stats()["pages"] == seen_first


def test_changed_file_reembeds_only_itself(tree, idx):
    import numpy as np

    class Emb:
        def __init__(self):
            self.texts = []

        def embed(self, texts, query: bool = False):
            self.texts.extend(texts)
            return np.ones((len(texts), 4), dtype=np.float32)

    c = LocalCrawler(idx, tree, embedder=Emb())
    run_all(c, c.start())
    (tree / "guide.md").write_text("completely rewritten guide")
    emb = Emb()
    c2 = LocalCrawler(idx, tree, embedder=emb)
    run_all(c2, c2.start())
    assert emb.texts and all("rewritten" in t or "guide.md" in t
                             for t in emb.texts)
