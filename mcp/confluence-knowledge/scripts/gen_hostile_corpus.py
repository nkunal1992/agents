#!/usr/bin/env python3
"""Deterministic hostile corpus generator — the shapes real Confluence
sites carry that synthetic happy-path fixtures never have.

Writes a fake-DC fixture (same shape as public_corpus.json) covering:
  - deep nesting (8 levels)
  - >200 children under one parent (pagination boundary)
  - empty body, macro-only body, malformed XHTML
  - a 900-row table, a 1 MB body, CJK page, duplicate titles
  - a page listed as a child but whose body is denied (pairs with
    FAKE_DC_FAULTS=403:content/9480)

  python scripts/gen_hostile_corpus.py --out tests/fixtures/hostile_corpus.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = "9000"
PAGES: dict[str, dict] = {}


def _add(pid: str, title: str, body: str, children: list[str]):
    PAGES[pid] = {"title": title, "body": body, "children": children}


def build() -> dict:
    deep = [str(9010 + i) for i in range(8)]
    wide = [str(9100 + i) for i in range(205)]
    _add(ROOT, "Hostile Root", "<p>root — negative-shape corpus</p>",
         deep[:1] + wide + ["9400", "9410", "9420", "9430", "9440",
                            "9450", "9460", "9470", "9480"])
    # chain of 8 — deep-nesting descent
    for i, pid in enumerate(deep):
        nxt = [deep[i + 1]] if i + 1 < len(deep) else []
        _add(pid, f"Deep Level {i + 1}",
             f"<p>level {i + 1} of 8 — deepest carries the fact</p>"
             + ("<p>BURIED_FACT: the vault code is 7-4-1-9.</p>"
                if i == len(deep) - 1 else ""), nxt)
    # 205 siblings — the pagination boundary (children cap is 200)
    for i, pid in enumerate(wide):
        _add(pid, f"Wide Sibling {i + 1:03d}",
             f"<p>sibling {i + 1} of 205 — page {pid} carries token "
             f"SIGIL-{i + 1:03d}</p>", [])
    # shape weirdos
    _add("9400", "Empty Page", "", [])
    _add("9410", "Macro Only",
         '<ac:structured-macro ac:name="code"><ac:parameter ac:name="lang">'
         "bash</ac:parameter><ac:plain-text-body><![CDATA[echo hi]]>"
         "</ac:plain-text-body></ac:structured-macro>", [])
    _add("9420", "Big Table",
         "<table>" + "".join(
             f"<tr><td>svc-{i:04d}</td><td>tcp/{9000 + i}</td></tr>"
             for i in range(900)) + "</table>", [])
    _add("9430", "Huge Body",
         "<p>" + "filler word " * 40000 + "</p>", [])
    _add("9440", "ページ",
         "<p>リージョンのデプロイは18です。</p>", [])
    _add("9450", "Duplicate Title", "<p>dup-a: port is 1111</p>", [])
    _add("9460", "Duplicate Title", "<p>dup-b: port is 2222</p>", [])
    _add("9470", "Malformed",
         "<p>unclosed <b>bold <i>nested" , [])
    _add("9480", "Listed-But-Denied",
         "<p>the body is here but listing returns 403</p>", [])
    return {"source": "synthetic hostile corpus",
            "attribution": "generated — no external content",
            "pages": PAGES}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(build()))
    print(f"{out}: {len(PAGES)} hostile pages")
