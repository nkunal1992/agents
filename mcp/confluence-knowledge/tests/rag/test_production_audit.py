import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import numpy as np
import pytest

from confluence_knowledge.client import AccessError, ConfluenceClient, EndpointRule, SecurityException
from confluence_knowledge.crawl import Crawler
from confluence_knowledge.extract import Chunk, extract_chunks, split_table_text
from confluence_knowledge.extract_files import extract_file_chunks
from confluence_knowledge.feedback import FeedbackStore
from confluence_knowledge.index import Index
from confluence_knowledge.retrieval import _sparse_rank, check_citations
from rag.audit_corpus import build_audit_corpus


def defect(number, raises=AssertionError):
    return pytest.mark.xfail(strict=True, raises=raises,
                             reason=f"#117 audit {number}: unresolved desired contract")


def put(idx, pid="1", text="old policy"):
    idx.upsert_page({"page_id": pid, "title": pid, "url": f"https://audit.invalid/{pid}"},
                    [Chunk(text, "", "text", 0, f"{pid}:{text}")], None)


@pytest.fixture
def index(tmp_path):
    idx = Index(tmp_path / "audit.db")
    yield idx
    idx.db.close()


def test_previous_snapshot_survives_unpublished_refresh(index):
    put(index)
    index.publish({"1"})
    put(index, text="new policy")
    assert [c["text"] for c in index.active_chunks()] == ["old policy"]


def test_independent_connection_sees_previous_snapshot(tmp_path):
    writer = Index(tmp_path / "two.db")
    reader = Index(tmp_path / "two.db")
    try:
        put(writer)
        writer.publish({"1"})
        put(writer, text="unpublished")
        assert [c["text"] for c in reader.active_chunks()] == ["old policy"]
    finally:
        reader.db.close()
        writer.db.close()


def test_transient_keep_set_survives_step_boundary(index):
    for pid in ("1", "2", "3"):
        put(index, pid)
    index.publish({"1", "2", "3"})

    class FakeCrawler(Crawler):
        def _page(self, pid):
            if pid == "2":
                raise AccessError("HTTP 503")
            return {"storage": f"<p>new {pid}</p>", "title": pid,
                    "url": "https://audit.invalid", "version": "2"}

        def _children(self, pid, is_folder=False):
            return [("2", "page"), ("3", "page")] if pid == "1" else []

    crawler = FakeCrawler(None, index, "cloud")
    job = crawler.start("1")
    crawler.step(job, batch=2)
    FakeCrawler(None, index, "cloud").step(job, batch=2)
    assert "2" in index.active_pages()


def test_superseded_crawler_cannot_publish(index):
    entered, release = threading.Event(), threading.Event()

    class Paused(Crawler):
        def _page(self, pid):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test synchronization timeout")
            return {"storage": "<p>obsolete job</p>", "title": pid, "url": "u", "version": "1"}

        def _children(self, pid, is_folder=False):
            return []

    crawler = Paused(None, index, "cloud")
    old = crawler.start("1")
    errors = []

    def run():
        try:
            crawler.step(old, batch=None)
        except (RuntimeError, sqlite3.Error) as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        crawler.start("2")
    finally:
        release.set()
        worker.join(5)
    assert not errors
    assert index.job(old)["state"] == "abandoned"


def test_superseded_job_cannot_write(index):
    """Write-time fencing: a job superseded mid-flight must not stage data —
    its upsert raises, so no staged garbage survives to a later publish."""
    from confluence_knowledge.index import StaleJobError
    index.create_job("j1", "root")
    index.save_job("j1", "running", ["page:1"], [])
    index.abandon_running_jobs()
    c = Chunk(text="x", heading_path="", kind="text", ordinal=0, chunk_id="c")
    with pytest.raises(StaleJobError):
        index.upsert_page({"page_id": "p", "title": "t", "url": "u"},
                          [c], None, None, job_id="j1")
    # scratch writes (job_id="") and running jobs are unaffected
    index.upsert_page({"page_id": "p", "title": "t", "url": "u"}, [c], None)
    index.create_job("j2", "root")
    index.save_job("j2", "running", ["page:1"], [])
    index.upsert_page({"page_id": "p", "title": "t", "url": "u"}, [c], None,
                      job_id="j2")


@defect("R14")
def test_first_crawl_resume_rejects_different_vector_dimension(index):
    class Tree(Crawler):
        def _page(self, pid):
            return {"storage": "<p>body</p>", "title": pid, "url": "u", "version": "1"}

        def _children(self, pid, is_folder=False):
            return [("2", "page")] if pid == "1" else []

    class Embedder:
        def __init__(self, dim):
            self.dim = dim

        def embed(self, texts):
            return np.ones((len(texts), self.dim), dtype=np.float32)

    a = Tree(None, index, "cloud", embedder=Embedder(4))
    job = a.start("1")
    a.step(job, batch=1)
    Tree(None, index, "cloud", embedder=Embedder(8)).step(job, batch=1)
    dimensions = {len(v) for v in index.vectors_for(
        [c["chunk_id"] for c in index.active_chunks()]).values()}
    assert len(dimensions) <= 1


@pytest.mark.parametrize("quote", ["", None])
def test_empty_or_missing_quote_cannot_verify(quote):
    mapping = {"claim": "policy permits everything", "chunk_id": "c"}
    if quote is not None:
        mapping["quote"] = quote
    assert not check_citations([{"chunk_id": "c", "text": "policy denies access"}],
                               [mapping])["ok"]


def test_verifier_is_provenance_not_entailment():
    result = check_citations([{"chunk_id": "c", "text": "Production deploys are prohibited."}],
                             [{"chunk_id": "c", "quote": "Production deploys are prohibited.",
                               "claim": "Production deploys are allowed."}])
    assert result["ok"]


@pytest.mark.parametrize("module", ["confluence_knowledge.server",
                                   "local_knowledge.server_local",
                                   "sharepoint_knowledge.server_sp"])
def test_evidence_handles_remain_unique_after_eviction(module, index, monkeypatch):
    server = pytest.importorskip(module)
    put(index)
    index.publish({"1"})
    monkeypatch.setattr(server, "_evidence_sets", {})
    monkeypatch.setattr(server, "_scope", lambda handle: {"key": "unique-scope"})
    monkeypatch.setattr(server, "_index_for", lambda key: index)
    monkeypatch.setattr(server, "_models_loaded", lambda wait: True)
    monkeypatch.setattr(server, "_embedder", lambda: type("E", (), {
        "embed": lambda self, texts: np.ones((len(texts), 4), dtype=np.float32)})())
    monkeypatch.setattr(server, "_reranker", lambda: None)
    monkeypatch.setattr(server, "_sparse", lambda: None)
    monkeypatch.setattr(server, "_feedback_for", lambda key: type("F", (), {
        "match": lambda self, vec: []})())
    monkeypatch.setattr(server, "retrieve", lambda *a, **kw: [])
    ids = [server.search("s", "q")["evidence_set"] for _ in range(40)]
    assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("module", ["confluence_knowledge.server",
                                   "local_knowledge.server_local",
                                   "sharepoint_knowledge.server_sp"])
def test_search_exposes_hunt_metadata_and_page_boundary_fetch(module, index, monkeypatch):
    """R05 — Failure Hunt probes need per-item metadata (kind/ordinal/
    page_id/version) and a page-boundary fetch; a bare chunk-id list cannot
    express 'first and last chunks of page P'."""
    server = pytest.importorskip(module)
    c1 = Chunk("alpha body", "", "text", 0, "p1:c0")
    c2 = Chunk("| h |\n| r |", "", "table", 1, "p1:c1")
    c3 = Chunk("tail end", "", "text", 2, "p1:c2")
    index.upsert_page({"page_id": "p1", "title": "Doc", "url": "u",
                       "version": "v42"}, [c1, c2, c3],
                      np.ones((3, 4), dtype=np.float32), None)
    index.publish({"p1"})
    monkeypatch.setattr(server, "_evidence_sets", {})
    monkeypatch.setattr(server, "_scope", lambda handle: {"key": "k"})
    monkeypatch.setattr(server, "_index_for", lambda key: index)
    monkeypatch.setattr(server, "_models_loaded", lambda wait: True)
    monkeypatch.setattr(server, "_embedder", lambda: type("E", (), {
        "embed": lambda self, texts: np.ones((len(texts), 4), dtype=np.float32)})())
    monkeypatch.setattr(server, "_reranker", lambda: None)
    monkeypatch.setattr(server, "_sparse", lambda: None)
    monkeypatch.setattr(server, "_feedback_for", lambda key: type("F", (), {
        "match": lambda self, vec: []})())
    res = server.search("s", "q")
    item = res["evidence"][0]
    for field in ("page_id", "kind", "ordinal", "page_version"):
        assert field in item, f"evidence item missing {field}"
    assert item["page_version"] == "v42"
    # boundary fetch: every chunk of the page, in ordinal order
    page_chunks = server.get_evidence("s", page_id="p1")["chunks"]
    assert [c["ordinal"] for c in page_chunks] == [0, 1, 2]
    assert page_chunks[-1]["text"] == "tail end"
    with pytest.raises(SecurityException):
        server.get_evidence("s", page_id="no-such-page")


def test_two_feedback_instances_do_not_lose_updates(tmp_path):
    a = FeedbackStore(tmp_path, "scope", "test")
    b = FeedbackStore(tmp_path, "scope", "test")
    a.add("a", np.ones(4), "correction a")
    b.add("b", np.ones(4), "correction b")
    assert FeedbackStore(tmp_path, "scope", "test").report()["count"] == 2


def test_two_feedback_processes_do_not_lose_updates(tmp_path):
    code = (
        "import sys; from pathlib import Path; import numpy as np; "
        "from confluence_knowledge.feedback import FeedbackStore; "
        "s=FeedbackStore(Path(sys.argv[1]), 'scope', 'test'); "
        "print('ready', flush=True); input(); s.add(sys.argv[2], np.ones(4), 'fix')"
    )
    processes = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), q],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True,
                                 env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                 for q in ("a", "b")]
    try:
        for p in processes:
            assert p.stdout.readline().strip() == "ready"
        for p in processes:
            _, stderr = p.communicate("\n", timeout=20)
            assert p.returncode == 0, stderr
        assert FeedbackStore(tmp_path, "scope", "test").report()["count"] == 2
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
                p.wait()


@pytest.mark.parametrize("data", [[{}], [{"qvec": [1, 0], "ts": 0}], [None]])
def test_malformed_feedback_entries_do_not_break_search(tmp_path, data):
    (tmp_path / "scope.feedback.json").write_text(json.dumps(data), encoding="utf-8")
    FeedbackStore(tmp_path, "scope", "test").match(np.ones(4))


@defect("R10")
def test_headerless_table_does_not_drop_first_service_row():
    chunks, _ = extract_chunks("<table><tr><td>svc-first</td><td>8000</td></tr>"
                               "<tr><td>svc-next</td><td>8001</td></tr></table>")
    assert any("row 1: svc-first | 8000" in c.text for c in chunks)


@defect("R10")
def test_rowspan_preserves_column_mapping():
    chunks, _ = extract_chunks("<table><tr><th>Region</th><th>Service</th><th>Port</th></tr>"
                               "<tr><td rowspan='2'>EU</td><td>alpha</td><td>8000</td></tr>"
                               "<tr><td>beta</td><td>8001</td></tr></table>")
    assert any("row 2: EU | beta | 8001" in c.text for c in chunks)


@defect("R10")
def test_markdown_table_has_structural_kind():
    chunks, _ = extract_file_chunks("table.md", b"| Service | Port |\n|---|---|\n|alpha|8000|")
    assert any(c.kind == "table" for c in chunks)


@defect("R10")
def test_single_wide_row_stays_within_claimed_word_budget():
    text = "Table — columns: key | description\nrow 1: alpha | " + "word " * 400
    assert all(len(c.split()) <= 280 for c in split_table_text(text))


def test_absolute_pagination_retains_query_token():
    import re
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"value": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = ConfluenceClient("https://graph.microsoft.com/v1.0", {},
                                   [EndpointRule(re.compile("sites/.*"), "audit")],
                                   session=http)
        client.get("https://graph.microsoft.com/v1.0/sites/s/lists/l/items?$skiptoken=next%2Bpage")
    assert "$skiptoken=next%2Bpage" in seen[0]


@defect("R11")
def test_evaluator_requires_all_required_pages(monkeypatch, tmp_path):
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path))
    path = Path(__file__).parents[2] / "scripts" / "eval_rag.py"
    spec = importlib.util.spec_from_file_location("audit_existing_eval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    score = module._score({"required_pages": ["Deploy", "Escalation"],
                           "required_facts": []},
                          [{"page_title": "Deploy", "page_url": "u", "text": "deployment"}])
    assert not score["recall_hit"]


def test_independent_corpus_is_balanced():
    pages, questions = build_audit_corpus()
    assert len(questions) == 60
    assert len({q["category"] for q in questions}) == 12
    assert len({p["id"] for p in pages}) == len(pages)
    assert all(set(q["required"]) <= {p["id"] for p in pages} for q in questions)


def test_zero_overlap_sparse_documents_do_not_vote_in_fusion():
    assert _sparse_rank({1: 1.0}, {"a": {2: 1.0}, "b": {3: 1.0}}) == []


@pytest.mark.parametrize("fmt", ["csv", "html", pytest.param("docx", marks=defect("R10")), "xlsx",
                                 pytest.param("pptx", marks=defect("R10"))])
def test_file_table_preserves_headers_and_structural_kind(fmt):
    import io
    rows = [["Service", "Port"]] + [[f"svc-{i:03}", str(8000+i)] for i in range(100)]
    output = io.BytesIO()
    if fmt == "csv":
        data = "\n".join(",".join(row) for row in rows).encode()
    elif fmt == "html":
        data = ("<table>" + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row)
                                   + "</tr>" for row in rows) + "</table>").encode()
    elif fmt == "docx":
        docx = pytest.importorskip("docx")
        doc = docx.Document()
        table = doc.add_table(rows=len(rows), cols=2)
        for i, row in enumerate(rows):
            for j, cell in enumerate(row):
                table.cell(i, j).text = cell
        doc.save(output)
        data = output.getvalue()
    elif fmt == "xlsx":
        lib = pytest.importorskip("openpyxl")
        book = lib.Workbook()
        for row in rows:
            book.active.append(row)
        book.save(output)
        book.close()
        data = output.getvalue()
    else:
        lib = pytest.importorskip("pptx")
        presentation = lib.Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        table = slide.shapes.add_table(len(rows), 2, 0, 0, 1000000, 1000000).table
        for i, row in enumerate(rows):
            for j, cell in enumerate(row):
                table.cell(i, j).text = cell
        presentation.save(output)
        data = output.getvalue()
    chunks, warns = extract_file_chunks(f"audit.{fmt}", data)
    assert not warns
    assert len(chunks) > 1
    assert all(c.kind == "table" and c.text.startswith("Table — columns: Service | Port")
               for c in chunks)
    assert any("svc-099 | 8099" in c.text for c in chunks)


@defect("R15", raises=sqlite3.OperationalError)
def test_vector_reads_respect_sqlite_parameter_limit(index):
    index.db.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 32766)
    assert index.vectors_for([f"chunk-{i}" for i in range(32767)]) == {}


def test_page_title_is_in_embedded_context(index):
    recorded = []

    class Embedder:
        def embed(self, texts):
            recorded.extend(texts)
            return np.ones((len(texts), 4), dtype=np.float32)

    class OnePage(Crawler):
        def _page(self, pid):
            return {"storage": "<p>Owner is OPER-42.</p>", "title": "Atlas ownership",
                    "url": "https://audit.invalid/1", "version": "1"}

        def _children(self, pid, is_folder=False):
            return []

    crawler = OnePage(None, index, "cloud", embedder=Embedder())
    crawler.step(crawler.start("1"))
    assert any("Atlas ownership" in text for text in recorded)


def test_token_aware_chunking_respects_model_budget():
    """R16: with a token measure, no produced chunk may exceed the token
    budget — words are a bad proxy for tokens on technical content."""
    def tokens(text):
        return len(text.split()) * 2   # fake: every word costs 2 tokens

    text = "<p>" + " ".join(f"w{i}" for i in range(400)) + "</p>"
    chunks, _ = extract_chunks(text, measure=tokens, budget=200)
    assert len(chunks) > 1
    assert all(tokens(c.text) <= 200 for c in chunks)
    # table header preservation survives under the token budget too
    from confluence_knowledge.extract import split_table_text
    t = "Table — columns: k | v\n" + "\n".join(f"row {i}: a{i} | b{i}"
                                              for i in range(60))
    segs = split_table_text(t, budget=200, measure=tokens)
    assert all(s.startswith("Table — columns:") and tokens(s) <= 200
               for s in segs)


def test_audit_metrics_distinguish_partial_sources_and_wrong_facts(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    path = Path(__file__).parents[2] / "scripts" / "audit_rag.py"
    spec = importlib.util.spec_from_file_location("audit_metrics", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pair = {"required": ["a", "b"], "facts": ["alpha", "beta"]}
    result = module.metrics(pair, [{"page_id": "a", "text": "alpha"},
                                   {"page_id": "b", "text": "wrong detail"}])
    assert result["source_recall"] == 1
    assert result["all_sources"]
    assert result["precision"] == 0.5
    assert not result["facts"]
    partial = module.metrics(pair, [{"page_id": "a", "text": "alpha"}])
    assert partial["source_recall"] == 0.5 and not partial["all_sources"]
    absent = module.metrics({"required": [], "facts": []}, [{"page_id": "a", "text": "unspecified"}])
    assert absent["nonempty_unanswerable"] and absent["facts"] is None
