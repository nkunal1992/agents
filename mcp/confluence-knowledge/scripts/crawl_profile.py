#!/usr/bin/env python3
"""Crawl-phase profiler — the work-laptop diagnostic for 'is a slow
crawl expected?'

    confluence-knowledge crawl-profile [--root <pageId-or-url>] \
        [--pages 25]

Runs a bounded crawl on a subtree of the configured profile and prints
the per-phase breakdown the daemon now records:

    fetch    — network time inside the fetch workers (page body +
               child listing)
    wait     — main thread blocked on the fetch window (fetch-bound
               signal: high wait means pages arrive slower than we
               process them)
    extract  — storage-XHTML → chunks (CPU)
    embed    — active dense-model ONNX vectors (CPU — usually the dominant cost on
               dense pages)
    sparse   — lexical sparse vectors (second model, optional)
    store    — sqlite staging writes
    battery_saver — true → embeds intentionally run ~half speed on
               battery; plug in for ~2x

Interpretation cheat-sheet:
  embed ≫ others → CPU-bound: expected on dense pages. On a
  roomy machine (≥16 GB, ≥8 cores) these env knobs typically buy 2-4x:
  KNOWLEDGE_MAX_BATCH=16  (chunks per ONNX call — default 4)
  KNOWLEDGE_THREADS=8     (ONNX intra-op threads — default cores/4)
  KNOWLEDGE_BATTERY_SAVER=0 (opt out of the 50% duty cycle on battery)
  wait/fetch high → network-bound: corporate
  proxy/VPN/DC latency; concurrency already adapts. store high → disk.

Needs a configured profile (`confluence-knowledge setup` or the env-var
credential path) and real models. Writes nothing outside the index.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None,
                    help="page id or viewpage URL — defaults to the "
                         "profile's configured root or first space root")
    ap.add_argument("--pages", type=int, default=25,
                    help="max pages to crawl (bounded step)")
    args = ap.parse_args()

    from confluence_knowledge import server
    from confluence_knowledge.client import make_client
    from confluence_knowledge.settings import SiteProfile

    profile = SiteProfile.load()
    if profile is None:
        sys.exit("no profile — run `confluence-knowledge setup` first")
    token = os.environ.get("CONF_KNOWLEDGE_TOKEN")
    if not token:
        from confluence_knowledge.auth import get_token
        token = get_token(profile.credential_ref)
    if not token:
        sys.exit("no token — CONF_KNOWLEDGE_TOKEN env or keychain")

    client = make_client(profile, token)
    root = args.root
    if root is None:
        # pick the first space root page the API reports
        try:
            pages = client.get(
                ("wiki/api/v2/pages" if profile.env == "cloud"
                 else "rest/api/content"), {"limit": 1})
            results = pages.get("results", [])
            if not results:
                sys.exit("could not discover a root page — pass --root")
            root = str(results[0]["id"])
        except Exception as e:  # noqa: BLE001
            sys.exit(f"root discovery failed: {e} — pass --root")
    # a URL form also works — the server's connect parses it
    root_arg = (root if str(root).startswith("http")
                else f"{profile.base_url}/pages/viewpage.action"
                     f"?pageId={root}" if profile.env == "datacenter"
                else root)

    c = server.connect(root_arg)
    scope = c["scope_handle"]
    j = server.start_crawl(scope)
    jid = j["job_id"]

    # bounded crawl — step() processes `batch` pages per call
    t0 = time.monotonic()
    processed = 0
    while processed < args.pages:
        st = server.crawl_step(scope, jid, batch=args.pages - processed)
        processed += max(0, st.get("pages_seen", 0) - processed) \
            if st.get("pages_seen") else st.get("processed", 0)
        if st["state"] != "running" or processed >= args.pages:
            break
        time.sleep(0.2)
    wall = time.monotonic() - t0

    idx = server._index_for_scope(scope) \
        if hasattr(server, "_index_for_scope") else None
    stats = None
    if idx is not None:
        p = idx.job_progress(jid)
        stats = p.get("stats")

    print(f"\ncrawled ~{processed} pages in {wall:.1f}s "
          f"({processed / max(wall, 0.1):.2f} pages/s)")
    if stats:
        tot = sum(v for k, v in stats.items() if k.endswith("_ms")) or 1
        print(f"\n{'phase':<10} {'seconds':>8} {'share':>6}")
        for k in ("fetch_ms", "wait_ms", "extract_ms", "embed_ms",
                  "sparse_ms", "store_ms"):
            if stats.get(k):
                v = stats[k]
                print(f"{k[:-3]:<10} {v / 1000:>8.1f} "
                      f"{v * 100 // tot:>5}%")
        print(f"\nchunks: {stats.get('chunks')} | "
              f"battery_saver: {stats.get('battery_saver')}")
    else:
        print("\nno stats recorded — upgrade to a build with "
              "stats:{job_id} instrumentation (#211)")
    # leave the job running in the background if it isn't done — the
    # daemon reclaims it; the profiled pages stay staged.
    return 0


if __name__ == "__main__":
    sys.exit(main())
