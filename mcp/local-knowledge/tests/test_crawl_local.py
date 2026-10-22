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
    # excluded + unsupported disclosed — secrets land in job warnings (not
    # coverage-degrading errors), parse issues in that page's warnings
    assert "secret" in " ".join(r["warnings"])
    warn_pages = [w for p in idx.db.execute(
        "SELECT warnings FROM pages").fetchall() for w in json.loads(p[0])]
    assert any("unsupported" in w for w in warn_pages)


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

        def embed(self, texts):
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

        def embed(self, texts):
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
