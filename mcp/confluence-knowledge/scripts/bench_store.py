#!/usr/bin/env python3
"""Store-phase isolation bench (#255): per-page commits vs one flush txn.

    python scripts/bench_store.py [--pages 200] [--chunks 8] [--repeat 3]

Today's flush calls ``index.upsert_page()`` per page — each call opens
``BEGIN IMMEDIATE`` and commits, so a 200-page crawl pays ~200 fsyncs.
The B2 lever is the same statements inside ONE transaction per flush.

This script measures the delta on synthetic but shape-real rows
(384-dim f32 vectors, ~80-term sparse payloads) WITHOUT touching the
engine: the "bulk" variant replays upsert_page's statements inside a
single _LockedRetry block. If the delta is small, B2 is dead on
evidence and we don't build it.

  per-page   — upsert_page() once per page (current shape)
  bulk       — identical inserts, one transaction for the whole flush
  copy-live  — copy_live_to_stage() per page (refresh-crawl path)
  copy-bulk  — equivalent row copies inside one transaction
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _synth(pages: int, chunks: int, seed: int = 7):
    """Shape-real synthetic rows → (page_dicts, chunk_lists, vecs, sparse)."""
    import numpy as np
    from confluence_knowledge.extract import Chunk

    rng = np.random.default_rng(seed)
    page_dicts, chunk_lists, vec_rows, sparse_rows = [], [], [], []
    for p in range(pages):
        pid = f"page-{p:05d}"
        page_dicts.append({
            "page_id": pid, "parent_id": "root", "title": f"Page {p}",
            "url": f"http://x/{pid}", "level": 1, "version": "1",
            "content_hash": f"hash-{p}", "warnings": []})
        cl = []
        for c in range(chunks):
            ck = Chunk(text=f"body text for chunk {c} of page {p} " * 12,
                       heading_path=f"Section {c}", kind="text", ordinal=c)
            ck.chunk_id = f"{pid}:h{p & 0xff:02x}:{c}"
            cl.append(ck)
        chunk_lists.append(cl)
        vec_rows.append(rng.random((chunks, 384), dtype=np.float32))
        sparse_rows.append([
            {int(t): float(w) for t, w in
             zip(rng.integers(0, 2_000_000, size=80),
                 rng.random(80).astype(np.float32))}
            for _ in range(chunks)])
    return page_dicts, chunk_lists, vec_rows, sparse_rows


def _fresh_index():
    from confluence_knowledge.index import Index
    return Index(Path(tempfile.mkdtemp(prefix="store-bench-")) / "idx.db")


def _bulk_upsert(idx, job_id: str, page_dicts, chunk_lists, vec_rows,
                 sparse_rows) -> None:
    """What a batched upsert_pages() would do — identical statements,
    one transaction for the whole buffer instead of one per page."""
    import numpy as np
    from confluence_knowledge.index import _LockedRetry

    with idx.lock, _LockedRetry(idx.db):
        row = idx.db.execute("SELECT state FROM jobs WHERE job_id=?",
                             (job_id,)).fetchone()
        if row is None or row[0] != "running":
            from confluence_knowledge.index import StaleJobError
            raise StaleJobError(f"job {job_id} not running")
        if vec_rows:
            idx.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                           (f"build:{job_id}", str(vec_rows[0].shape[1])))
        for page, chunks, vecs, sp in zip(page_dicts, chunk_lists,
                                          vec_rows, sparse_rows):
            idx.db.execute(
                "INSERT OR REPLACE INTO pages_stage VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, page["page_id"], page.get("parent_id"), page["title"],
                 page["url"], page.get("level", 0), page.get("version"),
                 time.time(), page.get("content_hash", ""),
                 json.dumps(page.get("warnings", []))))
            idx.db.execute(
                "DELETE FROM chunks_stage WHERE job_id=? AND page_id=?",
                (job_id, page["page_id"]))
            idx.db.executemany(
                "INSERT INTO chunks_stage VALUES (?,?,?,?,?,?,?)",
                [(job_id, c.chunk_id, page["page_id"], c.heading_path,
                  c.kind, c.ordinal, c.text) for c in chunks])
            idx.db.executemany(
                "INSERT INTO chunks_fts_stage VALUES (?,?,?)",
                [(job_id, c.chunk_id, c.text) for c in chunks])
            idx.db.executemany(
                "INSERT INTO vectors_stage VALUES (?,?,?)",
                [(job_id, c.chunk_id, vecs[i].astype(np.float32).tobytes())
                 for i, c in enumerate(chunks)])
            idx.db.executemany(
                "INSERT INTO sparse_vectors_stage VALUES (?,?,?)",
                [(job_id, c.chunk_id,
                  np.fromiter(sp[i].keys(), dtype=np.int64).tobytes()
                  + np.fromiter(sp[i].values(), dtype=np.float32).tobytes())
                 for i, c in enumerate(chunks)])


def _bulk_copy_live(idx, job_id: str, page_ids: list[str]) -> None:
    """Bulk analogue of copy_live_to_stage — one txn for all pages."""
    from confluence_knowledge.index import _LockedRetry

    with idx.lock, _LockedRetry(idx.db):
        for pid in page_ids:
            page = idx.db.execute(
                "SELECT page_id,parent_id,title,url,level,version,"
                "fetched_at,content_hash,warnings FROM pages "
                "WHERE page_id=? AND active=1", (pid,)).fetchone()
            idx.db.execute(
                "INSERT OR REPLACE INTO pages_stage VALUES (?,?,?,?,?,?,?,?,?,?)",
                (job_id, page[0], page[1], page[2], page[3], page[4],
                 page[5], time.time(), page[7], page[8]))
            idx.db.execute(
                "DELETE FROM chunks_stage WHERE job_id=? AND page_id=?",
                (job_id, pid))
            rows = idx.db.execute(
                "SELECT chunk_id,heading,kind,ordinal,text FROM chunks "
                "WHERE page_id=? AND active=1 ORDER BY ordinal",
                (pid,)).fetchall()
            idx.db.executemany(
                "INSERT INTO chunks_stage VALUES (?,?,?,?,?,?,?)",
                [(job_id, cid, pid, h, k, o, t) for cid, h, k, o, t in rows])
            idx.db.executemany(
                "INSERT INTO chunks_fts_stage VALUES (?,?,?)",
                [(job_id, cid, t) for cid, _h, _k, _o, t in rows])
            idx.db.executemany(
                "INSERT INTO vectors_stage VALUES (?,?,?)",
                [(job_id, cid, v) for cid, v in idx.db.execute(
                    "SELECT v.chunk_id,v.vec FROM vectors v JOIN chunks c "
                    "ON c.chunk_id=v.chunk_id WHERE c.page_id=?",
                    (pid,))])
            idx.db.executemany(
                "INSERT INTO sparse_vectors_stage VALUES (?,?,?)",
                [(job_id, cid, p) for cid, p in idx.db.execute(
                    "SELECT s.chunk_id,s.payload FROM sparse_vectors s "
                    "JOIN chunks c ON c.chunk_id=s.chunk_id "
                    "WHERE c.page_id=?", (pid,))])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=200)
    ap.add_argument("--chunks", type=int, default=8)
    ap.add_argument("--repeat", type=int, default=3)
    args = ap.parse_args()

    page_dicts, chunk_lists, vec_rows, sparse_rows = _synth(
        args.pages, args.chunks)
    n_chunks = args.pages * args.chunks
    out = {"pages": args.pages, "chunks": n_chunks, "repeat": args.repeat}

    for variant in ("per-page", "bulk"):
        ts = []
        for r in range(args.repeat):
            idx = _fresh_index()
            job = f"job-{variant}-{r}"
            idx.create_job(job, "root")
            idx.save_job(job, "running", [], [])
            t0 = time.monotonic()
            if variant == "per-page":
                for i, page in enumerate(page_dicts):
                    idx.upsert_page(page, chunk_lists[i], vec_rows[i],
                                    sparse_rows[i], job_id=job)
            else:
                _bulk_upsert(idx, job, page_dicts, chunk_lists,
                             vec_rows, sparse_rows)
            ts.append(time.monotonic() - t0)
        out[f"{variant}_s"] = [round(t, 3) for t in ts]
        out[f"{variant}_best_s"] = round(min(ts), 3)

    # refresh path: publish the staged snapshot, then re-stage-copy it
    idx = _fresh_index()
    job = "seed-job"
    idx.create_job(job, "root")
    idx.save_job(job, "running", [], [])
    _bulk_upsert(idx, job, page_dicts, chunk_lists, vec_rows, sparse_rows)
    all_ids = {p["page_id"] for p in page_dicts}
    idx.publish(all_ids, job_id=job)
    for variant in ("copy-live", "copy-bulk"):
        ts = []
        for r in range(args.repeat):
            job2 = f"copy-{variant}-{r}"
            idx.create_job(job2, "root")
            idx.save_job(job2, "running", [], [])
            t0 = time.monotonic()
            if variant == "copy-live":
                for p in page_dicts:
                    idx.copy_live_to_stage(p["page_id"], job2)
            else:
                _bulk_copy_live(idx, job2,
                                [p["page_id"] for p in page_dicts])
            ts.append(time.monotonic() - t0)
            for t in ("pages_stage", "chunks_stage", "vectors_stage",
                      "sparse_vectors_stage", "chunks_fts_stage"):
                idx.db.execute(f"DELETE FROM {t} WHERE job_id=?", (job2,))  # noqa: E501
            idx.db.commit()
        out[f"{variant}_s"] = [round(t, 3) for t in ts]
        out[f"{variant}_best_s"] = round(min(ts), 3)

    if out["bulk_best_s"] > 0:
        out["upsert_speedup"] = round(out["per-page_best_s"]
                                      / out["bulk_best_s"], 2)
    if out["copy-bulk_best_s"] > 0:
        out["copy_speedup"] = round(out["copy-live_best_s"]
                                    / out["copy-bulk_best_s"], 2)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
