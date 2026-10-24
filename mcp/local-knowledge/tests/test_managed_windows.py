"""#260 managed-laptop suite — local-knowledge half: OneDrive files-
on-demand placeholders. A managed laptop's Known Folder Move puts
Documents on OneDrive with placeholders that are NOT symlinks; reading
one hydrates it → the crawl freezes on a hydrate-storm. The crawler
must skip them with a disclosure warning.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from local_knowledge.crawl_local import LocalCrawler

_real_stat = Path.stat


def _patched_stat(marked: set[str]):
    """stat() that reports `marked` filenames as files-on-demand
    placeholders — copies every st_* attribute (lstat routes through
    stat in py3.13, so a bare namespace breaks is_symlink)."""
    def stat(s, **k):
        r = _real_stat(s, **k)
        attrs = {a: getattr(r, a) for a in dir(r) if a.startswith("st_")}
        if s.name in marked:
            attrs["st_file_attributes"] = 0x400000   # RECALL_ON_DATA_ACCESS
        return SimpleNamespace(**attrs)
    return stat


def test_cloud_only_detects_placeholder_attrs(tmp_path):
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"x")
    crawler = LocalCrawler.__new__(LocalCrawler)
    with mock.patch.object(Path, "stat", _patched_stat({"doc.pdf"})):
        assert crawler._cloud_only(f)
        assert not crawler._cloud_only(tmp_path / "other.txt")


def test_children_skips_placeholders_with_warning(tmp_path):
    (tmp_path / "local.txt").write_text("on disk")
    (tmp_path / "cloud.docx").write_text("placeholder")
    crawler = LocalCrawler.__new__(LocalCrawler)
    crawler.root = tmp_path
    with mock.patch.object(Path, "stat", _patched_stat({"cloud.docx"})):
        entries, warns = crawler._children(tmp_path)
    assert any("local.txt" in e for e in entries)
    assert not any("cloud.docx" in e for e in entries)
    assert any("cloud placeholder" in w for w in warns)


def test_read_placeholder_discloses_not_reads(tmp_path):
    f = tmp_path / "cloud.txt"
    f.write_bytes(b"hydrate me")
    crawler = LocalCrawler.__new__(LocalCrawler)
    crawler.root = tmp_path
    with mock.patch.object(Path, "stat", _patched_stat({"cloud.txt"})):
        meta, data, _ = crawler._read(f)
    assert data == b""                        # never hydrated
    assert any("cloud placeholder" in w for w in meta["warnings"])
