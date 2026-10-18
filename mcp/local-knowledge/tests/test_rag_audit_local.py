from confluence_knowledge.index import Index

from local_knowledge.crawl_local import LocalCrawler


def test_unreadable_directory_preserves_previous_content(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "runbook.txt").write_text("rollback requires release approval", encoding="utf-8")
    idx = Index(tmp_path / "index.db")
    try:
        crawler = LocalCrawler(idx, root)
        crawler.step(crawler.start())
        assert idx.stats()["pages"] == 1
        monkeypatch.setattr(crawler, "_children", lambda path: ([], ["temporary read error"]))
        crawler.step(crawler.start())
        assert idx.stats()["pages"] == 1
    finally:
        idx.db.close()
