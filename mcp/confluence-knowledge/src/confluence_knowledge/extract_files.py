"""Bytes → chunks for office/text file formats.

Shared by the local-files and SharePoint connectors — both end up with the
same Chunk objects the index and retriever already understand. Office/PDF
parsers are imported lazily so this module costs nothing when a file type
isn't used. Unsupported/binary files produce a warning, never a crash.
"""

from __future__ import annotations

import csv
import io
import zipfile

from .extract import MAX_WORDS, Chunk, _word_count, extract_chunks, split_oversized, split_table_text
from .settings import SetupError

MAX_FILE_WORDS = 200_000   # ~1MB of text — beyond that, index metadata only

SUPPORTED = {".txt", ".md", ".markdown", ".adoc", ".asciidoc", ".asc",
             ".csv", ".html", ".htm", ".docx", ".pptx", ".xlsx", ".pdf"}

# Names that look like credentials/secrets — excluded by default and
# disclosed in coverage warnings.
SECRET_NAMES = {".env", ".envrc", "id_rsa", "id_dsa", "id_ed25519", "id_ecdsa",
                "credentials", "credentials.json", ".netrc", ".pgpass"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}


def is_secret_name(filename: str) -> bool:
    """True when the filename strongly suggests a credential/secret file."""
    from pathlib import Path as P
    name = P(filename).name.lower()
    return (name in SECRET_NAMES
            or name.startswith(".env.") or name.endswith(".env")
            or P(name).suffix in SECRET_SUFFIXES)


def _need(pkg: str, fmt: str):
    raise SetupError(
        f"'{pkg}' isn't installed — install the file extra: "
        f"`pip install confluence-knowledge[files]` (needed for {fmt})")


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _text_chunks(text: str, heading: str, kind: str = "text",
                 measure=_word_count, budget: int = MAX_WORDS,
                 offset: int = 0) -> list[Chunk]:
    """Split plain text into bounded chunks (same shape as extract.py).
    `offset` carries the ordinal sequence across repeated calls into a
    shared list — each call restarts at `offset`, not 0 (#207: docx/
    pptx/pdf/md flush loops produced duplicate ordinals → UNIQUE
    constraint on chunks_stage)."""
    return [Chunk(seg, heading, kind, offset + i)
            for i, seg in enumerate(split_oversized(text, budget, measure))
            if seg.strip()]


def _docx_chunks(data: bytes, measure=_word_count,
                 budget: int = MAX_WORDS) -> list[Chunk]:
    try:
        import docx  # python-docx
    except ImportError:
        _need("python-docx", ".docx")
    doc = docx.Document(io.BytesIO(data))
    chunks: list[Chunk] = []
    heading: list[str] = []
    buf: list[str] = []

    def flush():
        text = "\n".join(buf).strip()
        buf.clear()
        if text:
            chunks.extend(_text_chunks(text, " > ".join(heading),
                                       measure=measure, budget=budget, offset=len(chunks)))

    # top-level children only — iterating descendants would also emit the
    # w:p inside table cells as prose, breaking the table-only kind contract
    for el in doc.element.body:
        tag = el.tag.split("}")[-1]
        if tag == "p":
            # find the paragraph object text via docx API per element
            texts = [n.text for n in el.iter() if n.tag.split("}")[-1] == "t" and n.text]
            line = "".join(texts).strip()
            if not line:
                continue
            style = el.find("{*}pPr/{*}pStyle")
            sty = style.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "") if style is not None else ""
            if sty.lower().startswith("heading"):
                flush()
                level = int(sty[7] or 1) if len(sty) > 7 else 1
                heading = heading[: level - 1]
                while len(heading) < level - 1:
                    heading.append("")
                heading.append(line)
            buf.append(line)
        elif tag == "tbl":
            flush()
            rows = []
            # direct w:tr/w:tc children only — descendant iter() re-walks
            # every nested table's rows as if they were the outer table's,
            # and nested cells as extra outer cells: a small nested-table
            # doc combinatorially explodes into tens of thousands of
            # duplicated chunks (#242). Nested text still lands once via
            # tc.iter("t") inside its enclosing cell.
            for tr in el.findall(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tr"):
                cells = []
                for tc in tr.findall(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}tc"):
                    cells.append("".join(t.text or "" for t in tc.iter(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t")).strip())
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                header, *body = rows
                t = "\n".join([f"Table — columns: {header}"]
                              + [f"row {i}: {r}" for i, r in enumerate(body, 1)])
                for seg in split_table_text(t, budget, measure):
                    chunks.append(Chunk(seg, " > ".join(heading), "table",
                                        len(chunks)))
    flush()
    return chunks


def _pptx_chunks(data: bytes, measure=_word_count,
                 budget: int = MAX_WORDS) -> list[Chunk]:
    try:
        from pptx import Presentation
    except ImportError:
        _need("python-pptx", ".pptx")
    prs = Presentation(io.BytesIO(data))
    chunks: list[Chunk] = []
    for i, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                texts.extend(p.text for p in shape.text_frame.paragraphs)
            if shape.has_table:
                # table contract — header row preserved, kind=table (R10)
                rows = [" | ".join(c.text for c in row.cells)
                        for row in shape.table.rows]
                rows = [r for r in rows if r.strip(" |")]
                if rows:
                    header, *body = rows
                    t = "\n".join([f"Table — columns: {header}"]
                                  + [f"row {n}: {r}" for n, r in
                                     enumerate(body, 1)])
                    for seg in split_table_text(t, budget, measure):
                        chunks.append(Chunk(seg, f"Slide {i}", "table",
                                            len(chunks)))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            texts.append("Notes: " + slide.notes_slide.notes_text_frame.text)
        text = "\n".join(t for t in texts if t and t.strip())
        if text.strip():
            chunks.extend(_text_chunks(text, f"Slide {i}",
                                       measure=measure, budget=budget, offset=len(chunks)))
    return chunks


def _xlsx_chunks(data: bytes, measure=_word_count,
                 budget: int = MAX_WORDS) -> list[Chunk]:
    try:
        import openpyxl
    except ImportError:
        _need("openpyxl", ".xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    chunks: list[Chunk] = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            vals = [str(c) if c is not None else "" for c in row]
            if any(v.strip() for v in vals):
                rows.append(" | ".join(vals))
        if not rows:
            continue
        header, *body = rows
        text = "\n".join([f"Table — columns: {header}"]
                         + [f"row {i}: {r}" for i, r in enumerate(body, 1)])
        for seg in split_table_text(text, budget, measure):
            chunks.append(Chunk(seg, f"Sheet: {ws.title}", "table",
                                len(chunks)))
    wb.close()
    return chunks


def _pdf_chunks(data: bytes, measure=_word_count,
                budget: int = MAX_WORDS) -> tuple[list[Chunk], list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError:
        _need("pypdf", ".pdf")
    chunks: list[Chunk] = []
    warns: list[str] = []
    reader = PdfReader(io.BytesIO(data))
    for i, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 — a bad page shouldn't kill the file
            warns.append(f"page {i}: no extractable text (scanned/image?)")
            continue
        if text.strip():
            chunks.extend(_text_chunks(text, f"Page {i}",
                                       measure=measure, budget=budget, offset=len(chunks)))
    if not chunks and not warns:
        warns.append("no extractable text — likely a scanned document (OCR not supported)")
    return chunks, warns


def _md_chunks(data: bytes, measure=_word_count,
               budget: int = MAX_WORDS) -> list[Chunk]:
    """Markdown: contiguous `|` runs are tables — they keep the table
    contract (header row, kind=table); everything else is prose (R10)."""
    text = _decode_text(data)
    chunks: list[Chunk] = []
    prose: list[str] = []

    def flush():
        body = "\n".join(prose).strip()
        prose.clear()
        if body:
            chunks.extend(_text_chunks(body, "", measure=measure,
                                       budget=budget, offset=len(chunks)))

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            # drop separator rows (|---|---|)
            rows = [r for r in rows
                    if r.strip("| -:") or r.count("|") > 2]
            rows = [r for r in rows if not set(r) <= set("| -:")]
            if rows:
                flush()
                header, *body = rows
                t = "\n".join([f"Table — columns: {header}"]
                              + [f"row {n}: {r}" for n, r in
                                 enumerate(body, 1)])
                for seg in split_table_text(t, budget, measure):
                    chunks.append(Chunk(seg, "", "table", len(chunks)))
            continue
        prose.append(lines[i])
        i += 1
    flush()
    return chunks


def _csv_chunks(data: bytes, measure=_word_count,
                budget: int = MAX_WORDS) -> list[Chunk]:
    text = _decode_text(data)
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        return []
    header, *body = rows
    out = "\n".join([f"Table — columns: {' | '.join(header)}"]
                    + [f"row {i}: {' | '.join(r)}" for i, r in enumerate(body, 1)])
    return [Chunk(seg, "", "table", i)
            for i, seg in enumerate(split_table_text(out, budget, measure))]


def extract_file_chunks(filename: str, data: bytes, measure=_word_count,
                        budget: int = MAX_WORDS) -> tuple[list[Chunk], list[str]]:
    """Dispatch on extension → (chunks, warnings). Never guesses at binaries.
    `measure`/`budget` bound chunk size — token-aware when a tokenizer-backed
    measure is supplied (R16)."""
    from pathlib import Path as P
    ext = P(filename).suffix.lower()
    if ext not in SUPPORTED:
        return [], [f"unsupported type '{ext or filename}' — skipped"]

    words = len(data) // 6  # rough guard against absurd inputs
    if words > MAX_FILE_WORDS * 6:  # bytes→words estimate too big
        return [], [f"file too large ({len(data)} bytes) — skipped"]

    try:
        if ext in (".txt", ".adoc", ".asciidoc", ".asc"):
            # adoc is plain-text markup — its `=` headers stay inline in
            # the prose (searchable); no full Asciidoctor render needed
            return _text_chunks(_decode_text(data), "",
                                measure=measure, budget=budget), []
        if ext in (".md", ".markdown"):
            return _md_chunks(data, measure, budget), []
        if ext in (".html", ".htm"):
            return extract_chunks(_decode_text(data), measure, budget)
        if ext == ".csv":
            return _csv_chunks(data, measure, budget), []
        if ext == ".docx":
            return _docx_chunks(data, measure, budget), []
        if ext == ".pptx":
            return _pptx_chunks(data, measure, budget), []
        if ext == ".xlsx":
            return _xlsx_chunks(data, measure, budget), []
        if ext == ".pdf":
            return _pdf_chunks(data, measure, budget)
    except zipfile.BadZipFile:
        return [], ["corrupt office file (not a valid zip) — skipped"]
    except Exception as e:  # noqa: BLE001 — one bad file must not kill a crawl
        return [], [f"parse error: {type(e).__name__}: {e}"]
    return [], [f"unsupported type '{ext}' — skipped"]
