#!/usr/bin/env python3
"""One-time corpus harvester — pull a REAL public Confluence subtree
(anonymous REST read) into a fixture file the fake-DC server can replay.

    python scripts/harvest_public_corpus.py --space KAFKA \
        --pages 200 --out tests/fixtures/public_corpus.json

Output: {"pages": {id: {"title":..., "body": <storage-format html>,
        "children": [ids...], "version":...}}, "source_space":...,
        "attribution":...} — re-parented under a synthetic root "ROOT".

License note: Apache project wikis are Apache-2.0/CC-BY licensed.
The fixture is vendored test data — fixtures/ATTRIBUTION.md records
the source. Do not harvest private or license-restricted spaces.
"""

import argparse
import json
import sys
import time
import urllib.request

API = "https://cwiki.apache.org/confluence/rest/api/content"


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--space", default="KAFKA")
    ap.add_argument("--pages", type=int, default=200)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default=API)
    args = ap.parse_args()

    pages: dict[str, dict] = {}
    start = 0
    while len(pages) < args.pages:
        url = (f"{args.base}?spaceKey={args.space}&limit=50&start={start}"
               "&expand=body.storage,ancestors,version")
        d = fetch(url)
        results = d.get("results", [])
        if not results:
            break
        for r in results:
            body = r.get("body", {}).get("storage", {}).get("value", "")
            if not body.strip():
                continue                       # blank/stub pages
            parent = (r.get("ancestors") or [{}])[-1].get("id")
            pages[r["id"]] = {
                "title": r["title"], "body": body,
                "parent": parent,
                "version": r.get("version", {}).get("number", 1)}
        start += len(results)
        time.sleep(0.15)                       # be polite to the real wiki
        if start > d.get("size", 0) and len(results) < 50:
            break

    if len(pages) < 10:
        print(f"only {len(pages)} pages harvested — space too small?")
        return 1

    # re-parent onto a synthetic root: orphan parents (outside the
    # harvest window) hang directly under ROOT
    corpus: dict[str, dict] = {"ROOT": {"title": f"{args.space} Docs Root",
                                        "body": "<p>Harvested public "
                                                f"corpus — space "
                                                f"{args.space}</p>",
                                        "children": []}}
    children_of: dict[str, list] = {}
    for pid, p in pages.items():
        par = p["parent"] if p["parent"] in pages else "ROOT"
        children_of.setdefault(par, []).append(pid)
        corpus[pid] = {"title": p["title"], "body": p["body"],
                       "version": p["version"]}
    for pid, kids in children_of.items():
        corpus.setdefault(pid, {"title": pid, "body": ""})
        corpus[pid]["children"] = kids
    corpus["ROOT"].setdefault("children", children_of.get("ROOT", []))

    out = {"source": f"cwiki.apache.org space {args.space}",
           "attribution": ("Apache Software Foundation documentation — "
                           "Apache-2.0/CC-BY; vendored for testing only"),
           "pages": corpus}
    with open(args.out, "w") as f:
        json.dump(out, f)
    print(f"harvested {len(pages)} real pages from {args.space} "
          f"→ {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
