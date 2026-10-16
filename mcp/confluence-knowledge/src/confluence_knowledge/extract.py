"""Storage-format XHTML → semantic chunks.

Local parse only (no external entity/network resolution), heading-aware block
splitting with bounded overlap. Page content is data, never instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

MAX_WORDS = 280
OVERLAP_WORDS = 40


@dataclass
class Chunk:
    text: str
    heading_path: str   # "Parent section > Sub"
    kind: str           # text | table
    ordinal: int
    chunk_id: str = ""  # assigned at index time: page_id:hash:ordinal


def _table_to_text(tbl) -> str:
    rows = []
    for tr in tbl.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append(" | ".join(cells))
    if not rows:
        return ""
    header, *body = rows
    out = [f"Table — columns: {header}"]
    out += [f"row {i}: {r}" for i, r in enumerate(body, 1)]
    return "\n".join(out)


def extract_chunks(storage_html: str) -> tuple[list[Chunk], list[str]]:
    """Return (chunks, extraction warnings)."""
    soup = BeautifulSoup(storage_html, "html.parser")  # stdlib parser; no ext entities
    warnings: list[str] = []

    for tag in soup.find_all(["script", "style", "ac:structured-macro"]):
        if tag.name.startswith("ac:"):
            warnings.append("skipped unsupported macro (content may be incomplete)")
        tag.decompose()

    chunks: list[Chunk] = []
    buf: list[str] = []
    heading_path: list[str] = []

    def flush(kind: str = "text"):
        text = "\n".join(buf).strip()
        buf.clear()
        if not text:
            return
        words = text.split()
        if len(words) <= MAX_WORDS:
            chunks.append(Chunk(text, " > ".join(heading_path), kind, len(chunks)))
            return
        # split on word boundaries with overlap
        step = MAX_WORDS - OVERLAP_WORDS
        for i in range(0, len(words), step):
            seg = " ".join(words[i:i + MAX_WORDS])
            if seg.strip():
                chunks.append(Chunk(seg, " > ".join(heading_path), kind, len(chunks)))

    for el in soup.find_all(re.compile(r"^h[1-6]$|^p$|^ul$|^ol$|^table$|^pre$|^blockquote$")):
        if el.name and el.name.startswith("h"):
            flush()
            level = int(el.name[1])
            title = el.get_text(" ", strip=True)
            heading_path = heading_path[: level - 1]
            while len(heading_path) < level - 1:
                heading_path.append("")
            heading_path.append(title)
            buf.append(f"## {title}")
        elif el.name == "table":
            flush()
            t = _table_to_text(el)
            if t:
                chunks.append(Chunk(t, " > ".join(heading_path), "table", len(chunks)))
        else:
            buf.append(el.get_text(" ", strip=True))
    flush()
    return chunks, warnings
