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


def _word_count(text: str) -> int:
    return len(text.split())


def _pack_units(units: list[str], budget: int, measure, overlap: int = 0,
                joiner: str = " ") -> list[str]:
    """Greedy pack `units` (words or lines) into segments of ≤budget units of
    `measure`. A single unit over budget is emitted whole (atomicity beats
    the limit — a lone table row must not be torn apart)."""
    segs, cur, size = [], [], 0
    for u in units:
        u_size = measure(u)
        if cur and size + u_size > budget:
            segs.append(joiner.join(cur))
            if overlap and joiner == " ":
                cur, size = cur[-overlap:], sum(
                    measure(x) for x in cur[-overlap:])
            else:
                cur, size = [], 0
        cur.append(u)
        size += u_size
    if cur:
        segs.append(joiner.join(cur))
    return segs


def split_oversized(text: str, budget: int = MAX_WORDS, measure=_word_count,
                    overlap_words: int = OVERLAP_WORDS) -> list[str]:
    """Split long text into ≤budget segments with overlap. With the default
    word measure this is the historical ≤280-word behaviour; with a token
    measure the bound is enforced in model units."""
    if measure(text) <= budget:
        return [text]
    return _pack_units(text.split(), budget, measure, overlap_words)


@dataclass
class Chunk:
    text: str
    heading_path: str   # "Parent section > Sub"
    kind: str           # text | table
    ordinal: int
    chunk_id: str = ""  # assigned at index time: page_id:hash:ordinal


def _span(raw) -> int:
    try:
        return max(1, int(raw)) if raw else 1
    except (TypeError, ValueError):
        return 1


def _table_to_text(tbl) -> str:
    """Grid-aware table → text. Rowspan/colspan cells carry their value
    into the covered columns of following rows; a table without <th> cells
    keeps all rows as data instead of eating the first as a header (R10)."""
    has_th = bool(tbl.find("th"))
    carry: dict[int, tuple[str, int]] = {}  # col → (text, rows remaining)
    grid: list[list[str]] = []
    for tr in tbl.find_all("tr"):
        row: list[str] = []
        col = 0
        for cell in tr.find_all(["th", "td"], recursive=False):
            while col in carry:
                row.append(carry[col][0])
                carry[col] = (carry[col][0], carry[col][1] - 1)
                if carry[col][1] <= 0:
                    del carry[col]
                col += 1
            txt = cell.get_text(" ", strip=True)
            for _ in range(_span(cell.get("colspan"))):
                row.append(txt)
                if _span(cell.get("rowspan")) > 1:
                    carry[col] = (txt, _span(cell.get("rowspan")) - 1)
                col += 1
        while col in carry or (carry and col <= max(carry)):
            row.append(carry.get(col, ("", 0))[0])
            if col in carry:
                carry[col] = (carry[col][0], carry[col][1] - 1)
                if carry[col][1] <= 0:
                    del carry[col]
            col += 1
        if any(row):
            grid.append(row)
    if not grid:
        return ""
    lines = [" | ".join(r) for r in grid]
    # <td>-only first rows are ambiguous (header or data) — keep them in
    # both places: a columns line for context AND row 1 so no data is lost
    body = lines[1:] if has_th else lines
    out = [f"Table — columns: {lines[0]}"]
    out += [f"row {i}: {r}" for i, r in enumerate(body, 1)]
    return "\n".join(out)


def split_table_text(t: str, budget: int = MAX_WORDS,
                     measure=_word_count) -> list[str]:
    """An oversized table becomes several ≤budget chunks, each carrying
    the header line — one unbounded chunk would exceed the embedder and the
    reranker's token limits and silently lose its tail rows."""
    if measure(t) <= budget:
        return [t]
    lines = t.split("\n")
    header, rows = lines[0], lines[1:]
    hw = measure(header)
    segs, buf, w = [], [], hw + 2
    for r in rows:
        rw = measure(r)
        if rw > budget - hw - 2:
            # a single row exceeds the budget on its own — split the row
            # itself so no emitted segment breaks the bound (R10)
            if buf:
                segs.append(header + "\n" + "\n".join(buf))
                buf, w = [], hw + 2
            for piece in split_oversized(r, max(1, budget - hw - 2), measure):
                segs.append(header + "\n" + piece)
            continue
        if buf and w + rw > budget:
            segs.append(header + "\n" + "\n".join(buf))
            buf, w = [], hw + 2
        buf.append(r)
        w += rw
    if buf:
        segs.append(header + "\n" + "\n".join(buf))
    return segs


def extract_chunks(storage_html: str, measure=_word_count,
                   budget: int = MAX_WORDS,
                   page_title: str = "") -> tuple[list[Chunk], list[str]]:
    """Return (chunks, extraction warnings). `measure`/`budget` bound chunk
    size — word count by default, model tokens when a tokenizer-backed
    measure is supplied (token-aware chunking, audit R16)."""
    soup = BeautifulSoup(storage_html, "html.parser")  # stdlib parser; no ext entities
    warnings: list[str] = []

    # macros that render external/dynamic content — skip entirely
    SKIP_MACROS = {"jira", "jirachart", "rss", "include", "widget", "gadget",
                   "livesearch", "recently-updated", "view-file", "gallery",
                   "content-by-label", "children", "pagetree", "excerpt-include",
                   "multiexcerpt-include"}
    skipped_macros: set[str] = set()
    for tag in soup.find_all(["script", "style", "ac:structured-macro"]):
        if tag.name.startswith("ac:"):
            name = tag.get("ac:name", "")
            if name in SKIP_MACROS:
                skipped_macros.add(name)
                tag.decompose()
                continue
            # container macros (code, expand, info, panel...) — keep the body:
            # rich-text bodies are unwrapped so their markup chunks normally;
            # plain-text bodies (code) become <pre> so the walker keeps order
            rich = tag.find("ac:rich-text-body")
            plain = tag.find("ac:plain-text-body")
            if plain is not None:
                pre = soup.new_tag("pre")
                pre.string = plain.get_text(strip=True)
                tag.replace_with(pre)
            elif rich is not None:
                rich.unwrap()
                tag.unwrap()
            else:
                tag.decompose()
            continue
        tag.decompose()
    if skipped_macros:
        warnings.append(
            "skipped dynamic macros (content may be incomplete): "
            + ", ".join(sorted(skipped_macros)))

    chunks: list[Chunk] = []
    buf: list[str] = []
    heading_path: list[str] = []

    def flush(kind: str = "text"):
        text = "\n".join(buf).strip()
        buf.clear()
        if not text:
            return
        for seg in split_oversized(text, budget, measure):
            chunks.append(Chunk(seg, " > ".join(heading_path), kind,
                                len(chunks)))

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
            if not t:
                continue            # gridless/empty table → no chunks
            for seg in split_table_text(t, budget, measure):
                chunks.append(Chunk(seg, " > ".join(heading_path), "table",
                                    len(chunks)))
        else:
            buf.append(el.get_text(" ", strip=True))
    flush()
    if page_title:
        # every chunk carries the page title — retrieval matches
        # title-bearing queries ("Deploy Windows") on any chunk, the way
        # every RAG indexer treats title+body as one passage. NB the
        # heading loop reuses `title` as a local — keep the page's own
        # name distinct or it gets shadowed by the last heading seen.
        for c in chunks:
            c.text = f"## {page_title}\n{c.text}"
    return chunks, warnings
