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
