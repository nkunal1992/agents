"""Storage-format extraction: headings, paragraphs, tables, macro gaps."""

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


def test_long_text_splits_with_overlap():
    big = "<p>" + " ".join(f"w{i}" for i in range(900)) + "</p>"
    chunks, _ = extract_chunks(big)
    assert len(chunks) > 1
    # overlap: chunk 1 end words appear at chunk 2 start
    w1 = chunks[0].text.split()
    assert chunks[1].text.split()[:10] == w1[-50:-40] or \
        chunks[1].text.split()[0] in w1
