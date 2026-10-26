"""Crawl-speed benchmark — generates a realistic docx/adoc corpus and runs
a REAL crawl against the cached ONNX models (~/.knowledge/models).

    python mcp/local-knowledge/scripts/bench_corpus.py [--files N] [--keep]

Reports wall-clock, pages/sec, chunk counts, and the per-phase stats the
crawler persists (fetch/extract/embed/sparse/store/wait). Use it to prove
a pathological or docx-heavy folder indexes in seconds per page, not
minutes (#244, #242).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def _gen_corpus(root: Path, files: int) -> dict:
    """Mixed-shape corpus: nested-table docx, wide-table docx, paragraph
    docx, heading/docx, adoc + md + txt. Returns {path: expect_chunks_hint}."""
    import docx
    made = {}
    docx_dir = root / "office"
    nested_dir = root / "nested"
    adoc_dir = root / "wiki"
    for d in (docx_dir, nested_dir, adoc_dir):
        d.mkdir(parents=True)

    # ~1/3 nested-table docs — the #242 pathology: every row carries a
    # nested 8x3 table (generated-manual shape)
    n_nested = max(2, files // 3)
    for i in range(n_nested):
        d = docx.Document()
        d.add_heading(f"Manual {i}", 0)
        for s in range(4):
            d.add_heading(f"Section {s}", 2)
            d.add_paragraph("lorem ipsum " * 40)
        t = d.add_table(rows=1, cols=2)
        for r in range(30):
            row = t.add_row()
            row.cells[0].text = f"key{r}"
            inner = row.cells[1].add_table(rows=4, cols=3)
            for irow in inner.rows:
                for c in irow.cells:
                    c.text = "value " * 8
        bio = io.BytesIO(); d.save(bio)
        (nested_dir / f"nested{i}.docx").write_bytes(bio.getvalue())
        made[str(nested_dir / f"nested{i}.docx")] = "nested-table"

    # ~1/3 wide flat tables — big legit tables
    n_wide = max(2, files // 3)
    for i in range(n_wide):
        d = docx.Document()
        d.add_heading(f"Report {i}", 0)
        d.add_paragraph("summary " * 80)
        t = d.add_table(rows=1, cols=6)
        for r in range(200):
            row = t.add_row()
            for j, c in enumerate(row.cells):
                c.text = f"r{r}c{j} " + "data " * 6
        bio = io.BytesIO(); d.save(bio)
        (docx_dir / f"wide{i}.docx").write_bytes(bio.getvalue())
        made[str(docx_dir / f"wide{i}.docx")] = "wide-table"

    # ~1/3 paragraph-heavy docs
    n_par = files - n_nested - n_wide
    for i in range(max(1, n_par)):
        d = docx.Document()
        d.add_heading(f"Guide {i}", 0)
        for s in range(20):
            d.add_heading(f"Part {s}", 2)
            for _ in range(6):
                d.add_paragraph("paragraph text " * 30)
        bio = io.BytesIO(); d.save(bio)
        (docx_dir / f"prose{i}.docx").write_bytes(bio.getvalue())
        made[str(docx_dir / f"prose{i}.docx")] = "prose"

    # a few adoc + md + txt (the user's other formats)
    for i in range(6):
        (adoc_dir / f"page{i}.adoc").write_text(
            f"= Page {i}\n\n" + "\n".join(
                f"== Section {s}\n\ntext " * 1 + "word " * 40
                for s in range(5)), encoding="utf-8")
        (adoc_dir / f"note{i}.md").write_text(
            f"# Note {i}\n\n" + "\n".join(
                "- " + "item " * 10 for _ in range(30)), encoding="utf-8")
        (adoc_dir / f"plain{i}.txt").write_text(
            "\n".join("plain line " + "word " * 10 for _ in range(50)), encoding="utf-8")
    return made


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=18,
                    help="number of docx to generate (mixed shapes)")
    ap.add_argument("--keep", action="store_true",
                    help="keep the generated corpus dir")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="lk-bench-"))
    corpus = work / "corpus"
    corpus.mkdir()
    made = _gen_corpus(corpus, args.files)
    total_files = len(list(corpus.rglob("*.*")))
    print(f"corpus: {total_files} files under {corpus} "
          f"({sum(v == 'nested-table' for v in made.values())} nested-table, "
          f"{sum(v == 'wide-table' for v in made.values())} wide-table, "
          f"{sum(v == 'prose' for v in made.values())} prose docx + adoc/md/txt)")

    # real daemon-off crawl, real models at ~/.knowledge/models
    os.environ["LOCAL_KNOWLEDGE_DAEMON"] = "0"
    os.environ.setdefault("LOCAL_KNOWLEDGE_HOME", str(work / "home"))
    os.environ.setdefault("LOCAL_KNOWLEDGE_WORKDIR", str(work / "wdir"))

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    sys.path.insert(0, str(Path(__file__).parent.parent.parent
                            / "confluence-knowledge" / "src"))
    from local_knowledge import server_local

    t0 = time.monotonic()
    c = server_local.connect(str(corpus))
    print(f"connect: {c['kind']} — {corpus}")
    job = server_local.start_crawl(c["scope_handle"])
    last_note = ""
    while True:
        r = server_local.crawl_step(c["scope_handle"], job["job_id"])
        n = r.get("note") or ""
        if n and n != last_note:
            print(f"  … {n}")
            last_note = n
        if r["state"] != "running":
            break
        time.sleep(0.5)
    dt = time.monotonic() - t0
    key = c["scope_handle"].split(":")[1]
    idx = server_local._index_for(key)
    stats_raw = idx.meta_get(f"stats:{job['job_id']}")
    stats = json.loads(stats_raw) if stats_raw else {}
    pages = len(idx.active_pages())
    chunks_total = stats.get("chunks", 0)

    print(f"\n=== crawl {r['state']} in {dt:.1f}s ===")
    print(f"pages: {pages} | chunks: {chunks_total} | "
          f"pages/sec: {pages / max(dt, 0.01):.1f}")
    for k in ("fetch_ms", "extract_ms", "embed_ms", "sparse_ms",
              "store_ms", "wait_ms"):
        if k in stats:
            print(f"  {k:12s} {stats[k] / 1000:7.1f}s")
    if r.get("warnings"):
        print("warnings:", r["warnings"][:5])
    if r.get("errors"):
        print("errors:", r["errors"][:5])

    # a search to prove the index answers
    q = server_local.search(c["scope_handle"], "lorem ipsum paragraph")
    print(f"search evidence rows: {len(q.get('evidence', []))} "
          f"(coverage {q.get('coverage')})")

    # the pre-#242 cost projection: nested-table docs produced ~46k
    # chunks each — at this machine's measured embed rate that is the
    # reported ~30-minute stall on a single file
    if stats.get("embed_ms") and chunks_total:
        rate = chunks_total / (stats["embed_ms"] / 1000)
        proj = 46332 / rate
        print(f"\npre-fix projection: one 46,332-chunk doc at "
              f"{rate:.0f} chunks/s embeds in ~{proj / 60:.1f} min — "
              f"the reported stall")

    if not args.keep:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"corpus kept at {corpus}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
