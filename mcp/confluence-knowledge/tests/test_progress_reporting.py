"""#171: errors_n mirrors errors, pages_seen tracks fetch progress,
unchanged refresh counts as done, remaining zeroes at done, status caches
the identity check."""


import json

import httpx
import numpy as np
import pytest

from confluence_knowledge import server
from confluence_knowledge.client import RULES_DC, ConfluenceClient
from confluence_knowledge.crawl import Crawler
from confluence_knowledge.index import Index
from confluence_knowledge.jobs import Progress, crawl_control
from confluence_knowledge.settings import SiteProfile


def _client(handler):
    return ConfluenceClient("https://dc", {}, RULES_DC,
                            session=httpx.Client(
                                transport=httpx.MockTransport(handler)))


def _page(pid, ancestors=None):
    return {"id": pid, "title": f"Page {pid}",
            "body": {"storage": {"value": f"<p>content {pid}</p>"}},
            "version": {"number": 1},
            "ancestors": [{"id": a} for a in (ancestors or [])],
            "_links": {"webui": f"/p/{pid}", "base": "https://dc"}}


class _Embed:
    def embed(self, texts):
        return np.ones((len(texts), 4), dtype=np.float32)


def _tree_with_denied():
    """root 1 → kids 2..6; page 4's body is 403."""
    pages = {"1": _page("1")}
    children = {"1": [{"id": str(i)} for i in range(2, 7)]}
    for i in range(2, 7):
        pid = str(i)
        pages[pid] = _page(pid, ["1"])
        children[pid] = []

    def h(req):
        path = req.url.path
        if "/child/page" in path:
            pid = path.split("/content/")[1].split("/")[0]
            return httpx.Response(200, json={
                "results": children.get(pid, []),
                "size": len(children.get(pid, [])), "limit": 200})
        pid = path.split("/content/")[1].rstrip("/")
        if pid == "4":
            return httpx.Response(403)
        return httpx.Response(200, json=pages[pid])

    return _client(h)


def _controlled(tmp_path, client, monkeypatch):
    monkeypatch.setenv("CONF_KNOWLEDGE_FETCH_CONCURRENCY", "1")
    idx = Index(tmp_path / "s.db")
    prog = Progress()
    owner = server._owner
    c = Crawler(client, idx, "datacenter")
    job = c.start("1")
    idx.acquire_lease(job, owner.value, owner.pid)
    ctl = crawl_control(idx, job, prog, cancel_flag=None,
                        owner=owner, hb_s=60)
    c.control = ctl
    c._embedder = _Embed()
    return c, job, prog


def test_errors_n_remaining_and_pages_seen(tmp_path, monkeypatch):
    c, job, prog = _controlled(tmp_path, _tree_with_denied(), monkeypatch)
    r = c.step(job, batch=None)
    assert r["state"] == "done"
    assert prog.errors_n == len(r["errors"]) >= 1   # the 403 body error
    assert prog.pages_seen == 5     # staged pages; the denied one errors
    assert prog.remaining == 0
    snap = prog.snapshot()
    assert snap["errors_n"] == prog.errors_n
    assert snap["pages_seen"] == 5


def test_refresh_counts_reused_pages(tmp_path, monkeypatch):
    c, job, _ = _controlled(tmp_path, _tree_with_denied(), monkeypatch)
    assert c.step(job, batch=None)["state"] == "done"
    # second crawl, unchanged tree — reused rows must still count as done
    prog2 = Progress()
    job2 = c.start("1")
    idx = c.index
    idx.acquire_lease(job2, server._owner.value, server._owner.pid)
    c.control = crawl_control(idx, job2, prog2, cancel_flag=None,
                              owner=server._owner, hb_s=60)
    assert c.step(job2, batch=None)["state"] == "done"
    assert prog2.pages_done == 5      # 5 indexed pages reused, not "0 done"
    assert prog2.remaining == 0


# ---------------- status identity TTL ----------------


class _CountClient:
    def __init__(self):
        self.identity_calls = 0
        self.slow = False

    def get(self, path, params=None):
        if "user/current" in path:
            self.identity_calls += 1
            if self.slow:
                import time as _t
                _t.sleep(2)        # hostile DC — retry/hang territory
            return _Resp({"username": "t"})
        return _Resp({})


class _Resp:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d


@pytest.fixture
def wired_status(monkeypatch, tmp_path):
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path / "home"))
    profile = SiteProfile("datacenter", "https://c", None, "ref")
    monkeypatch.setattr(SiteProfile, "load",
                        staticmethod(lambda home: profile))
    client = _CountClient()
    monkeypatch.setattr(server, "_client_for", lambda p: client)
    monkeypatch.setattr(server, "_scopes", {})
    monkeypatch.setattr(server, "_indexes", {})
    monkeypatch.setattr(server, "_CREDS_CACHE", {})
    return client


def test_status_caches_identity_check(wired_status):
    client = wired_status
    server.status()
    server.status()
    server.status()
    assert client.identity_calls == 1   # TTL keeps the identity hit cheap


def test_connect_forces_revalidation(wired_status):
    client = wired_status
    server.status()
    server.status()
    # a connect (possible token rotation) must not serve a stale verdict
    c = server.connect("1")
    assert c["scope_handle"]
    server.status()
    assert client.identity_calls >= 2


def test_progress_for_derives_errors_n_from_job(tmp_path, monkeypatch):
    """The durable error list is authoritative — a snapshot persisted
    before the final appends can't leave errors_n stuck at 0."""
    monkeypatch.setattr(server, "_progress", {})
    idx = Index(tmp_path / "s.db")
    idx.create_job("j", "1")
    idx.save_job("j", "done", [], ["page 4: denied", "page 7: denied"])
    p = server._progress_for(idx, "j")
    assert p["errors_n"] == 2


def test_status_never_blocks_on_wire(wired_status, monkeypatch):
    """#173: a hostile /user/current must not turn status into a >2 s
    call — stale verdicts are served while a daemon thread refreshes."""
    import time as t
    client = wired_status
    server.status()                      # first call populates the cache
    monkeypatch.setattr(server, "_CREDS_TTL_S", 0.0)   # always stale
    client.slow = True                   # wire now takes 2 s per call
    t0 = t.monotonic()
    for _ in range(5):
        r = server.status()
        flag = next(c for c in r["checks"] if c["flag"] == "creds_valid")
        assert flag["ok"]                # last-known-good still served
    assert t.monotonic() - t0 < 2.0
    # the async refreshes dedupe — at most a couple of wire calls, not 5
    assert client.identity_calls <= 3


# ---------------- model-version lifecycle (#178) ----------------


def test_stale_model_notifies_and_autoreindexes(wired_status, monkeypatch):
    notes = []
    monkeypatch.setattr(server, "notify",
                        lambda t, b: notes.append((t, b)) or True)
    server.connect("1")
    key = next(iter(server._scopes.values()))["key"]
    idx = server._index_for(key)
    idx.set_model_id("ancient-model-v0")
    server._scopes.clear()
    r = server.connect("1")
    assert r["stale_model"] is True
    assert r["auto_reindex"] in ("started", "queued")
    assert notes and "model" in notes[0][1]
    # a second connect while the job runs must not stack a second worker
    r2 = server.connect("1")
    assert r2["auto_reindex"] in (None, "queued")


def test_fingerprint_covers_persisted_models():
    from confluence_knowledge.retrieval import EMBED_MODEL_ID, MODEL_MARKER, RERANK_REPO, SPARSE_MODEL_ID
    assert EMBED_MODEL_ID in MODEL_MARKER
    assert SPARSE_MODEL_ID in MODEL_MARKER
    # reranker deliberately absent — query-time only, must not force a
    # rebuild on upgrade
    assert RERANK_REPO not in MODEL_MARKER


def test_cross_window_progress_uses_durable_blob(tmp_path, monkeypatch):
    """#186: a non-owning process's zeroed Progress must not clobber the
    owner's checkpointed progress — pages_done stays real."""

    monkeypatch.setattr(server, "_progress", {})
    idx = Index(tmp_path / "s.db")
    idx.create_job("j", "1")
    idx.save_job("j", "running", ["a", "b"], [])
    # owner process checkpoints its live progress into the blob
    own = Progress()
    own.pages_done = 41
    own.pages_seen = 41
    own.set_phase("embedding")
    idx.meta_set("progress:j", json.dumps(own.snapshot()))
    # this process holds an empty Progress marked 'another IDE window'
    foreign = Progress()
    foreign.worker = "another server process"
    server._progress["j"] = foreign

    p = server._progress_for(idx, "j")
    assert p["pages_done"] == 41                      # not clobbered by 0
    assert p["worker"] == "another server process"   # ownership disclosed


def test_heartbeat_noop_without_wrapper():
    """heartbeat() is safe to call from sync context — no queue → no-op."""
    from confluence_knowledge.mcp_async import heartbeat
    heartbeat(1)  # must not raise


def test_heartbeat_emits_through_queue(tmp_path, monkeypatch):
    """Inside the wrapper the worker thread's heartbeat() lands in the
    queue the async pump drains — verify via the thread-local sink."""
    import queue

    from confluence_knowledge import mcp_async
    q = queue.Queue()
    mcp_async._hb_local.queue = q
    try:
        mcp_async.heartbeat(5, total=10, message="mid")
        assert q.get_nowait() == (5, 10, "mid")
    finally:
        mcp_async._hb_local.queue = None


def test_wait_for_crawl_default_fits_ide_timeout():
    """#186: the 50 s default blew past IDE client timeouts — the new
    default must sit under ~30 s."""
    import inspect
    sig = inspect.signature(server.wait_for_crawl)
    assert sig.parameters["wait_s"].default <= 30


# ---- #187 three-tier reporting ----


def test_pulse_line_format():
    from confluence_knowledge.reporting import pulse
    p = {"phase": "fetching", "pages_done": 120, "pages_seen": 130,
         "remaining": 40, "rate_ppm": 245.5, "errors_n": 2}
    s = pulse(p, rss_mb=1656.4)
    assert "fetching" in s and "120/~170" in s and "245.5 ppm" in s \
        and "1656 MB RSS" in s and "2 errors" in s


def test_stages_math():
    from confluence_knowledge.reporting import stages
    p = {"pages_seen": 80, "pages_done": 60, "remaining": 20,
         "eta_s": 300}
    st = stages(p)
    assert st["fetch"]["pages"] == 80 and st["embed"]["pages"] == 60
    assert st["pct_complete"] == 60.0 and st["eta_s"] == 300


def test_audit_groups_errors_by_cause(tmp_path):
    from confluence_knowledge.reporting import audit
    idx = Index(tmp_path / "a.db")
    idx.create_job("j", "1")
    errs = ["page 4: HTTP 403 denied", "page 9: HTTP 502 bad gateway",
            "page 7: HTTP 403 denied", "page 3: timeout after 30s"]
    idx.save_job("j", "done", [], errs)
    a = audit(idx, "j")
    assert a["errors_by_cause"]["access denied (HTTP 403)"] == 2
    assert a["errors_by_cause"]["server error (HTTP 502)"] == 1
    assert a["errors_by_cause"]["timeout"] == 1
    assert a["errors_total"] == 4
    assert "index_size_mb" in a


def test_progress_for_exposes_pulse_and_stages(tmp_path, monkeypatch):
    """Running jobs carry tier-1/2 fields; terminal jobs carry audit."""
    monkeypatch.setattr(server, "_progress", {})
    idx = Index(tmp_path / "s.db")
    idx.create_job("j", "1")
    idx.save_job("j", "running", ["a"], [])
    p = server._progress_for(idx, "j")
    assert "pulse" in p and "stages" in p
    idx.save_job("j", "done", [], [])
    p2 = server._progress_for(idx, "j")
    assert "summary" in p2 and p2["summary"]["state"] == "done"
