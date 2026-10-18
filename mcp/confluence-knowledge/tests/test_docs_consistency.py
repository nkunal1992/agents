"""Docs-vs-code drift guard.

Docs repeatedly went stale (model sizes, tool counts, leg counts,
thresholds). This test greps the docs for the facts most likely to drift
and asserts they match the code — a mismatch fails CI instead of
misleading a user.

Only hard invariants are asserted (exact numbers that appear in docs);
anything phrased loosely stays out of scope.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MCP = Path(__file__).resolve().parents[1] / "src"
SRC = MCP / "confluence_knowledge"
DOCS = [REPO / "docs" / f for f in (
    "knowledge-agent-tdd.md", "knowledge-agent-slides.md",
    "confluence-knowledge.md", "local-knowledge.md",
    "sharepoint-knowledge.md", "confluence-knowledge-explained.md",
    "confluence-knowledge-slides.md") if (REPO / "docs" / f).exists()]
DOCS += [REPO / "bundle" / "README.md", REPO / "README.md"]
DOCS = [d for d in DOCS if d.exists()]


def _const(name, path=None):
    src = (path or SRC / "retrieval.py").read_text()
    m = re.search(rf"^{name}\s*=\s*([\w_.'\"]+)", src, re.MULTILINE)
    assert m, f"{name} not found in {path}"
    return m.group(1).strip('"\'')


def test_doc_tool_lists_match_servers():
    """Any doc that enumerates the MCP tools must list the real set."""
    tools = set(re.findall(r"@mcp\.tool\(\)\s*\ndef\s+(\w+)",
                           (SRC / "server.py").read_text()))
    for d in DOCS:
        txt = d.read_text()
        for m in re.finditer(r"`(status|connect|start_crawl|crawl_step|search|"
                             r"get_evidence|verify_citations|submit_feedback|"
                             r"feedback_report)`", txt):
            assert m.group(1) in tools, f"{d.name}: tool `{m.group(1)}` not real"
        # if a doc lists verify_citations it should list the new tools too
        if "`verify_citations`" in txt and "narrow by design" in txt:
            assert "`submit_feedback`" in txt, f"{d.name}: tool list missing submit_feedback"
            assert "`feedback_report`" in txt, f"{d.name}: tool list missing feedback_report"


def test_doc_threshold_constants_match_code():
    """Numbers quoted in docs must equal code constants."""
    fb = (SRC / "feedback.py").read_text()
    checks = {
        "MIN_RERANK_SCORE": _const("MIN_RERANK_SCORE"),
        "TOP_K_EVIDENCE": _const("TOP_K_EVIDENCE"),
        "RERANK_MAX": _const("RERANK_MAX"),
        "RERANK_MAX_TOKENS": _const("RERANK_MAX_TOKENS"),
        "MAX_SEARCHABLE_CHUNKS": _const("MAX_SEARCHABLE_CHUNKS"),
    }
    min_sim = re.search(r"DEFAULT_MIN_SIM\s*=\s*([\d.]+)", fb).group(1)

    all_docs = "\n".join(d.read_text() for d in DOCS)
    # docs that cite a similarity threshold must cite the real one
    for m in re.finditer(r"cosine\s*[≥>=]+\s*([\d.]+)", all_docs):
        assert m.group(1) == min_sim, \
            f"docs say cosine ≥{m.group(1)} but DEFAULT_MIN_SIM={min_sim}"
    # evidence cap — docs cite the EVIDENCE_CHAR_CAP value
    srv = (SRC / "server.py").read_text()
    ev_cap = re.search(r"EVIDENCE_CHAR_CAP\s*=\s*(\d+)", srv).group(1)
    for m in re.finditer(r"capped at (\d+) chars", all_docs):
        assert m.group(1) == ev_cap, \
            f"docs say evidence capped at {m.group(1)} but code slices {ev_cap}"
    assert checks["MIN_RERANK_SCORE"] in ("0.02",), "noise floor changed — audit docs"


def test_doc_claims_no_two_leg_retrieval():
    """Retrieval is three legs (dense + sparse + FTS) — the pre-bm25 docs
    described two. Any remaining 'two' phrasing is drift."""
    for d in DOCS:
        txt = d.read_text()
        for bad in ("two ranked lists", "both indexes", "both the keyword",
                    "the two ways"):
            assert bad not in txt, f"{d.name}: stale two-leg claim: {bad!r}"


def test_doc_download_size_not_stale():
    """Model download size claims must be the current ~1.4 GB figure."""
    for d in DOCS:
        assert "1.3 GB" not in d.read_text(), \
            f"{d.name}: stale '1.3 GB' model-size claim"


def test_no_shared_feedback_claims():
    """Feedback is per-user per-machine — docs must not imply sharing."""
    for d in DOCS + list((REPO / "agents").glob("*.agent.md")):
        txt = d.read_text()
        for bad in ("colleague correction", "colleagues' fixes",
                    "anyone asks"):
            assert bad not in txt.lower() or bad == "colleague", \
                f"{d.name}: implies shared feedback: {bad!r}"
