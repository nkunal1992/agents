"""Storage-format extraction: headings, paragraphs, tables, macro gaps."""

import pytest

from confluence_knowledge.extract import extract_chunks

STORAGE = """
<h1>Release Process</h1>
<p>Releases ship on Tuesdays.</p>
<h2>Approvals</h2>
<p>Two approvals are required.</p>
<table><tr><th>Gate</th><th>Owner</th></tr>
<tr><td>QA</td><td>Priya</td></tr>
<tr><td>Sec</td><td>Marco</td></tr></table>
<ac:structured-macro ac:name="include"><ac:parameter ac:name="">x</ac:parameter></ac:structured-macro>
"""


def test_chunks_have_headings():
    chunks, _ = extract_chunks(STORAGE)
    assert any("Release Process" in c.heading_path for c in chunks)
    assert any("Approvals" in c.heading_path for c in chunks)


def test_table_preserved_with_columns():
    chunks, _ = extract_chunks(STORAGE)
    tbl = [c for c in chunks if c.kind == "table"]
    assert tbl and "Gate" in tbl[0].text and "Priya" in tbl[0].text


def test_macro_warned():
    _, warns = extract_chunks(STORAGE)
    assert any("macro" in w for w in warns)


MACROS = """
<p>Before</p>
<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[git push --tags]]></ac:plain-text-body></ac:structured-macro>
<ac:structured-macro ac:name="info"><ac:rich-text-body><p>Env must be prod.</p></ac:rich-text-body></ac:structured-macro>
<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">PCT-1</ac:parameter></ac:structured-macro>
<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">PCT-2</ac:parameter></ac:structured-macro>
<p>After</p>
"""


def test_container_macros_keep_body_content():
    chunks, _ = extract_chunks(MACROS)
    text = " ".join(c.text for c in chunks)
    assert "git push --tags" in text   # code macro body kept
    assert "Env must be prod" in text  # info macro body kept


def test_dynamic_macros_skipped_and_warned_once():
    chunks, warns = extract_chunks(MACROS)
    text = " ".join(c.text for c in chunks)
    assert "PCT-1" not in text and "PCT-2" not in text
    assert len([w for w in warns if "jira" in w]) == 1  # deduped, not per-tag


def test_long_text_splits_with_overlap():
    big = "<p>" + " ".join(f"w{i}" for i in range(900)) + "</p>"
    chunks, _ = extract_chunks(big)
    assert len(chunks) > 1
    # overlap: chunk 1 end words appear at chunk 2 start
    w1 = chunks[0].text.split()
    assert chunks[1].text.split()[:10] == w1[-50:-40] or \
        chunks[1].text.split()[0] in w1


def test_file_chunk_ordinals_never_repeat():
    """#207: _text_chunks restarts ordinals at 0 per call — docx/pptx/
    pdf/md flush loops extended a shared list, so paragraph-runs
    between tables all got ordinal 0 and the staged insert died on
    PRIMARY KEY (job_id, chunk_id). The user saw 'UNIQUE constraint
    failed: chunks_stage' on a docx folder."""
    import io
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_heading("Guide", 0)
    for i in range(6):
        d.add_paragraph(f"paragraph {i} of configuration guidance text")
        t = d.add_table(rows=2, cols=2)
        t.rows[0].cells[0].text = f"h{i}"
        t.rows[0].cells[1].text = f"h{i}b"
    buf = io.BytesIO()
    d.save(buf)
    from confluence_knowledge.extract_files import extract_file_chunks
    chunks, _ = extract_file_chunks("guide.docx", buf.getvalue())
    ords = [c.ordinal for c in chunks]
    assert len(ords) == len(set(ords)), \
        f"duplicate ordinals — stage insert would violate the PK: {ords}"


def test_md_chunk_ordinals_never_repeat():
    """#207: markdown sections hit the same per-call ordinal reset."""
    from confluence_knowledge.extract_files import extract_file_chunks
    md = "# A\n\nbody one with enough words\n\n# B\n\nbody two more text\n\n# C\n\nbody three trailing"
    chunks, _ = extract_file_chunks("g.md", md.encode())
    ords = [c.ordinal for c in chunks]
    assert len(ords) == len(set(ords)), \
        f"duplicate ordinals: {ords}"


def test_adoc_extracts_as_text():
    """#208/#231: AsciiDoc is plain-text markup — its `=` headers stay
    searchable inline; no full Asciidoctor render needed."""
    from confluence_knowledge.extract_files import SUPPORTED, extract_file_chunks
    for ext in (".adoc", ".asciidoc", ".asc"):
        assert ext in SUPPORTED
        chunks, warns = extract_file_chunks(
            f"deploy{ext}", b"= IDCT Guide\n\n== Setup\n\nrun the tool\n")
        assert chunks and not warns
        assert "Setup" in chunks[0].text or "Setup" in chunks[-1].text


def _nested_table_docx():
    import io

    docx = pytest.importorskip("docx")
    doc = docx.Document()
    tbl = doc.add_table(rows=1, cols=2)
    for r in range(60):
        row = tbl.add_row()
        row.cells[0].text = f"key{r}"
        inner = row.cells[1].add_table(rows=8, cols=3)
        for irow in inner.rows:
            for c in irow.cells:
                c.text = "value " * 10
    bio = io.BytesIO()
    doc.save(bio)
    return bio.getvalue()


def test_docx_nested_table_chunks_bounded():
    """#242: descendant iter() walked nested-table rows/cells as if they
    were the outer table's — a 39KB doc exploded into ~46k chunks and
    embedded for tens of minutes. Direct-children iteration keeps the
    count content-proportional; nested text lands once via tc.iter("t")."""
    from confluence_knowledge.extract_files import extract_file_chunks
    chunks, _ = extract_file_chunks("nested.docx", _nested_table_docx())
    # pre-fix produced 46,332 — bound it well under a sane multiple
    assert len(chunks) < 1500
    assert any("value" in c.text for c in chunks)


def test_table_with_oversized_header_row_does_not_fragment():
    """#242: a ~250-word header row leaves ~1 word of budget per piece —
    split_oversized fragmented every row into single-word chunks."""
    from confluence_knowledge.extract import split_table_text
    header = "Table — columns: " + " ".join(f"h{i}" for i in range(200))
    rows = [f"row{i}: " + " ".join(f"cell{j}" for j in range(200))
            for i in range(20)]
    segs = split_table_text("\n".join([header] + rows))
    assert len(segs) < 200
