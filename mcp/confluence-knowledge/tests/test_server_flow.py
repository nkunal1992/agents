"""End-to-end tool sequence the agent runs: connect -> start_crawl -> crawl_step.

Regression for #68: Index.__init__ left an uncommitted write transaction and
the server opened a fresh connection per tool call, so start_crawl
self-deadlocked with "database is locked".
"""

import sqlite3
import threading
import time
from typing import ClassVar

import pytest

from confluence_knowledge import server
from confluence_knowledge.client import SecurityException
from confluence_knowledge.settings import SiteProfile


class _Resp:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d


class _FakeClient:
    def get(self, path, params=None):
        if path.endswith("user/current"):
            return _Resp({"username": "tester"})
        if "/child/page" in path:
            return _Resp({"results": [], "size": 0, "limit": 200})
        return _Resp({"id": "1", "title": "Root",
                      "body": {"storage": {"value": "<h1>Hi</h1><p>text</p>"}},
                      "version": {"number": 1},
                      "_links": {"webui": "/x", "base": "https://c"}})


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("CONF_KNOWLEDGE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("CONF_KNOWLEDGE_HOME", str(tmp_path / "home"))
    profile = SiteProfile("datacenter", "https://c", None, "ref")
    monkeypatch.setattr(SiteProfile, "load", staticmethod(lambda home: profile))
    monkeypatch.setattr(server, "_client_for", lambda p: _FakeClient())
    monkeypatch.setattr(server, "_embedder", lambda: None)
    monkeypatch.setattr(server, "_reranker", lambda: None)
    monkeypatch.setattr(server, "_sparse", lambda: None)
    monkeypatch.setattr(server, "_models_ready", threading.Event())
    monkeypatch.setattr(server, "_warm_started", False)
    real_connect = sqlite3.connect
    # fail fast instead of waiting out the 30s busy timeout
    monkeypatch.setattr(sqlite3, "connect",
                        lambda *a, **kw: real_connect(*a, **{**kw, "timeout": 1}))
    monkeypatch.setattr(server, "_scopes", {})
    monkeypatch.setattr(server, "_indexes", {})
    monkeypatch.setattr(server, "_workers", {})
    monkeypatch.setattr(server, "_worker_errors", {})


def wait_done(scope, job_id, timeout=15):
    """The background worker does the crawling; crawl_step polls."""
    end = time.monotonic() + timeout
    while True:
        r = server.crawl_step(scope, job_id)
        if r["state"] != "running":
            return r
        assert time.monotonic() < end, "background crawl did not finish"
        time.sleep(0.02)


def test_connect_then_crawl_does_not_lock(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    r = wait_done(c["scope_handle"], job["job_id"])
    assert r["state"] == "done"
    assert r["pages_seen"] == 1


def test_background_crawl_worker_does_the_work(wired):
    """#81: start_crawl returns instantly and the crawl completes on its
    own — crawl_step is only an instant progress read, so a big subtree
    doesn't need one LLM turn per page."""
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    # the job may still be running — crawl_step must return instantly anyway
    t = time.monotonic()
    r = server.crawl_step(c["scope_handle"], job["job_id"])
    assert time.monotonic() - t < 2
    assert r["state"] in ("running", "done")
    r = wait_done(c["scope_handle"], job["job_id"])
    assert r["state"] == "done" and r["pages_seen"] == 1


class _FakeEmbedder:
    def embed(self, texts, query: bool = False):
        import numpy as np
        return np.ones((len(texts), 4), dtype=np.float32)


class _FakeReranker:
    def score(self, query, docs):
        import numpy as np
        # descending 0.9, 0.8, … — above the 0.3 evidence floor
        return np.linspace(0.9, 0.5, len(docs), dtype=np.float32)


def test_full_agent_flow_with_models(wired, monkeypatch):
    """connect → crawl (embedding on) → search (rerank on) → evidence → citations."""
    emb, rr = _FakeEmbedder(), _FakeReranker()
    monkeypatch.setattr(server, "_embedder", lambda: emb)
    monkeypatch.setattr(server, "_reranker", lambda: rr)
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    assert wait_done(c["scope_handle"], job["job_id"])["state"] == "done"

    res = server.search(c["scope_handle"], "text")
    assert res["evidence"], res
    top = res["evidence"][0]
    assert top["page_title"] == "Root" and top["page_url"]

    full = server.get_evidence(c["scope_handle"], [top["chunk_id"]])
    assert "text" in full["chunks"][0]["text"]

    ok = server.verify_citations(c["scope_handle"], res["evidence_set"], [
        {"claim": "c", "chunk_id": top["chunk_id"], "quote": "text"}])
    assert ok["ok"] is True


def test_search_during_crawl_says_crawling(wired, monkeypatch):
    # keep the worker parked on the model-warmup gate so the job stays
    # "running" deterministically: a never-set event AND warmup disabled —
    # otherwise the warmup thread sets the event, the worker finishes the
    # 1-page crawl, and search reaches retrieve instead of the early return
    monkeypatch.setattr(server, "_models_ready", threading.Event())
    monkeypatch.setattr(server, "_start_warmup", lambda: None)
    c = server.connect("1")
    server.start_crawl(c["scope_handle"])
    res = server.search(c["scope_handle"], "text")
    assert res["coverage"] == "crawling" and "crawl_step" in res["note"]


def test_crawl_step_returns_while_models_load(wired, monkeypatch):
    """The first torch/model load can exceed the IDE timeout — the tool must
    answer 'loading' instead of blocking."""
    # models never finish loading — the warmup gate stays closed so the
    # worker parks and the tool call must still answer instantly
    monkeypatch.setattr(server, "_start_warmup", lambda: None)
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    r = server.crawl_step(c["scope_handle"], job["job_id"])
    assert r["note"] == server.LOADING_NOTE


def test_scope_survives_server_restart(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    server._scopes.clear()                    # IDE restarted the server
    server._workers.clear()                   # and the worker died with it
    assert wait_done(c["scope_handle"], job["job_id"])["state"] == "done"


def test_scope_rebind_needs_no_network(wired, monkeypatch):
    """#229: a daemon restart drops _scopes — the rebind must not call
    connect()'s live credential check (VPN/network blip = 'rejecting the
    connected scope'). The scope's identity is proven by root_page meta
    connect() already stamped — locally verifiable, zero network."""
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    wait_done(c["scope_handle"], job["job_id"])
    server._scopes.clear()
    # every network path now dead — rebind must still serve reads
    def _no_network(*a, **k):
        raise AssertionError("rebind touched the network")
    monkeypatch.setattr(server, "_client_for", _no_network)
    s = server._scope(c["scope_handle"])
    assert s["key"] == c["scope_handle"].split(":")[1]
    # fabricated + malformed handles still rejected
    from confluence_knowledge.settings import SetupError
    key = c["scope_handle"].split(":")[1]
    for bad in (f"scope:{key}:999", "scope:deadbeef:1", "scope:x"):
        try:
            server._scope(bad)
            raise SystemExit(f"{bad} accepted")
        except SetupError:
            pass


def test_coverage_complete_despite_earlier_failed_job(wired, monkeypatch):
    emb, rr = _FakeEmbedder(), _FakeReranker()
    monkeypatch.setattr(server, "_embedder", lambda: emb)
    monkeypatch.setattr(server, "_reranker", lambda: rr)
    c = server.connect("1")
    server.start_crawl(c["scope_handle"])     # abandoned
    job = server.start_crawl(c["scope_handle"])
    wait_done(c["scope_handle"], job["job_id"])
    assert server.search(c["scope_handle"], 'say "text')["coverage"] == "complete"


def test_reconnect_after_crawl_sees_cache(wired):
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    wait_done(c["scope_handle"], job["job_id"])
    again = server.connect("1")
    assert again["cached"] is True
    assert again["cached_pages"] == 1


def test_verify_survives_restart(wired, monkeypatch):
    """#231: _evidence_sets is process memory — a daemon restart must not
    turn verify_citations into 'unknown evidence set'. The persisted
    shadow (chunk_ids + epoch in index meta) rebuilds the set."""
    emb, rr = _FakeEmbedder(), _FakeReranker()
    monkeypatch.setattr(server, "_embedder", lambda: emb)
    monkeypatch.setattr(server, "_reranker", lambda: rr)
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    wait_done(c["scope_handle"], job["job_id"])
    res = server.search(c["scope_handle"], "text")
    set_id = res["evidence_set"]
    q = res["evidence"][0]
    mapping = [{"claim": "x", "chunk_id": q["chunk_id"],
                "quote": q["text"][:12]}]
    server._evidence_sets.clear()            # daemon restarted
    v = server.verify_citations(c["scope_handle"], set_id, mapping)
    assert v["ok"], v


def test_orphan_reclaim_needs_no_network(wired, monkeypatch):
    """#231: daemon boot reclaims a 'running' job whose worker died — the
    scope_key check must not need a live API (offline daemon boot after
    a crash). connect() stamps `principal` — reclaim validates locally."""
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    key = c["scope_handle"].split(":")[1]
    server._workers.clear()
    def _no_network(*a, **k):
        raise AssertionError("reclaim touched the network")
    monkeypatch.setattr(server, "_client_for", _no_network)
    server._resume_job(key, job["job_id"])
    # _resume_job delegates to _ensure_worker — the registry key is
    # scope:job (the consistent scheme every spawn path shares)
    assert f"{key}:{job['job_id']}" in server._workers or wait_done(
        c["scope_handle"], job["job_id"])["state"] == "done"


def test_verify_citations_routes_to_daemon(monkeypatch, wired):
    """Evidence sets are process-local — under daemon mode `search`
    populates the DAEMON's registry, so verify must route there first.
    (#196 residual — proxy's _evidence_sets is always empty.)"""
    seen = {}
    monkeypatch.setattr(
        server, "_daemon_call",
        lambda name, args, timeout: (
            seen.update(name=name, args=args) or {"ok": True}))
    out = server.verify_citations("scope:x", "set:1", [])
    assert seen["name"] == "verify_citations"
    assert out == {"ok": True}            # daemon answered — no local check


class _TreeClient:
    """Static DC tree for #240 alias tests:
    1 "Root" → children 2 "Child" + 3 "Sibling"."""
    _PAGES: ClassVar = {
        "1": ("Root", "the root page", []),
        "2": ("Child", "child only content", ["1"]),
        "3": ("Sibling", "text text text text text", ["1"]),
    }
    _KIDS: ClassVar = {"1": ["2", "3"], "2": [], "3": []}

    def get(self, path, params=None):
        if "user/current" in path:
            return _Resp({"username": "u"})
        if "/child/page" in path:
            pid = path.split("/content/")[1].split("/child")[0]
            kids = self._KIDS[pid]
            return _Resp({"results": [{"id": k, "type": "page"}
                                      for k in kids],
                          "size": len(kids), "limit": 200})
        pid = path.rsplit("/", 1)[1]
        title, body, anc = self._PAGES[pid]
        return _Resp({"id": pid, "title": title,
                      "body": {"storage": {"value": f"<p>{body}</p>"}},
                      "version": {"number": 1},
                      "ancestors": [{"id": a} for a in anc],
                      "_links": {"webui": "/x", "base": "https://c"}})


def _tree_crawl(wired, monkeypatch):
    """connect("1") + crawl the 3-page tree to done; models faked."""
    monkeypatch.setattr(server, "_client_for", lambda p: _TreeClient())
    emb, rr = _FakeEmbedder(), _FakeReranker()
    monkeypatch.setattr(server, "_embedder", lambda: emb)
    monkeypatch.setattr(server, "_reranker", lambda: rr)
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    r = wait_done(c["scope_handle"], job["job_id"])
    assert r["state"] == "done" and r["pages_seen"] == 3
    return c


def test_descendent_scope_answers_from_ancestor(wired, monkeypatch):
    """#240: connect(B ⊂ indexed A) aliases reads into A's snapshot —
    no new crawl, evidence restricted to B's subtree."""
    _tree_crawl(wired, monkeypatch)
    c = server.connect("2")
    assert c["covered_by"] == "1" and c["cached"] is True
    assert c["cached_pages"] == 1
    res = server.search(c["scope_handle"], "text")
    assert res["evidence"]
    assert {e["page_id"] for e in res["evidence"]} == {"2"}
    # sibling pages are denied even by direct chunk/page request
    for pid in ("1", "3"):
        with pytest.raises(SecurityException):
            server.get_evidence(c["scope_handle"], page_id=pid)
    q = res["evidence"][0]
    v = server.verify_citations(c["scope_handle"], res["evidence_set"], [
        {"claim": "x", "chunk_id": q["chunk_id"],
         "quote": q["text"][:12]}])
    assert v["ok"], v


def test_descendent_alias_survives_restart(wired, monkeypatch):
    """#240+#229: the alias persists in the scope's own index meta —
    a daemon restart reconstructs the virtual binding offline."""
    _tree_crawl(wired, monkeypatch)
    c = server.connect("2")
    server._scopes.clear()
    server._evidence_sets.clear()
    res = server.search(c["scope_handle"], "text")
    assert res["evidence"] and \
        {e["page_id"] for e in res["evidence"]} == {"2"}
    q = res["evidence"][0]
    v = server.verify_citations(c["scope_handle"], res["evidence_set"], [
        {"claim": "x", "chunk_id": q["chunk_id"],
         "quote": q["text"][:12]}])
    assert v["ok"], v


def test_descendent_materializes_on_start_crawl(wired, monkeypatch):
    """#240: start_crawl on an aliased scope clones the subtree into its
    own index first — the alias clears and the scope becomes standalone."""
    _tree_crawl(wired, monkeypatch)
    c = server.connect("2")
    assert c.get("covered_by") == "1"
    job = server.start_crawl(c["scope_handle"])
    r = wait_done(c["scope_handle"], job["job_id"])
    assert r["state"] == "done"
    s = server._scopes[c["scope_handle"]]
    assert "reads_from" not in s
    res = server.search(c["scope_handle"], "text")
    assert {e["page_id"] for e in res["evidence"]} == {"2"}


def test_status_lists_persisted_scope_after_restart(wired, monkeypatch):
    """#263: a new chat (empty _scopes, stdio respawn, daemon restart)
    must see the persisted index — indexed_scopes carries the handle the
    disk re-bind path accepts."""
    c = _tree_crawl(wired, monkeypatch)
    server._scopes.clear()                    # simulate new chat / respawn
    st = server.status()
    scopes = st["indexed_scopes"]
    assert len(scopes) == 1
    assert scopes[0]["scope_handle"] == c["scope_handle"]
    assert scopes[0]["root_page_id"] == "1"
    assert scopes[0]["pages"] == 3
    assert scopes[0]["last_crawl"]["state"] == "done"


def test_search_without_handle_uses_single_index(wired, monkeypatch):
    """#263: 'ask a question in a new chat' — no URL, one index on disk →
    search uses it without the agent needing the handle."""
    _tree_crawl(wired, monkeypatch)
    server._scopes.clear()
    res = server.search("", "text")
    assert res["evidence"] and res["coverage"] in ("complete", "partial")


def test_search_without_handle_no_index(wired):
    """Nothing indexed → 'unconfigured' so the agent runs connect, not a
    fabricated search."""
    server._scopes.clear()
    res = server.search("", "anything")
    assert res["evidence"] == [] and res["coverage"] == "unconfigured"
    assert "connect" in res["note"]


def test_search_without_handle_ambiguous(wired, monkeypatch):
    """Two usable indexes → 'ambiguous' naming candidates — never a
    silent guess across subtrees."""
    _tree_crawl(wired, monkeypatch)

    class _SecondTree(_TreeClient):
        _PAGES: ClassVar = {"9": ("Nine", "nine body", [])}
        _KIDS: ClassVar = {"9": []}
    monkeypatch.setattr(server, "_client_for", lambda p: _SecondTree())
    c2 = server.connect("9")
    wait_done(c2["scope_handle"], server.start_crawl(c2["scope_handle"])["job_id"])
    server._scopes.clear()
    res = server.search("", "text")
    assert res["coverage"] == "ambiguous"
    assert {c["root_page_id"] for c in res["candidates"]} == {"1", "9"}
    # an explicit handle still works unchanged
    res2 = server.search(c2["scope_handle"], "nine")
    assert res2["evidence"]


def test_descendent_alias_needs_same_principal(wired, monkeypatch):
    """#240 security: an ancestor index built under a different account
    must never serve this connection — alias is refused, scope stays
    empty."""
    _tree_crawl(wired, monkeypatch)
    # rewrite A's principal stamp — simulates an index from another user
    a = server._scopes[server.connect("1")["scope_handle"]]["key"]
    server._index_for(a).meta_set("principal", "someone-else")
    c = server.connect("2")
    assert "covered_by" not in c and c["cached"] is False
    res = server.search(c["scope_handle"], "text")
    assert res["evidence"] == [] and res["coverage"] == "empty"


# ---- #283: daemon-unavailable fallback for read tools ----


def _daemon_dead(monkeypatch, reason="daemon spawn failed: boom"):
    """Daemon mode on, but the daemon never answers."""
    from confluence_knowledge.rpc import DaemonUnavailable
    monkeypatch.setenv("CONF_KNOWLEDGE_DAEMON", "1")
    monkeypatch.setattr(server, "_daemon_on", lambda: True)
    monkeypatch.setattr(
        server, "ensure_daemon",
        lambda rpc: (_ for _ in ()).throw(DaemonUnavailable(reason)))
    monkeypatch.setattr(server, "_rpc", lambda: None)


def test_daemon_call_returns_none_and_stashes_reason(monkeypatch):
    """The reason rides args so callers can disclose it (#253 diagnosis
    kept) while callers fall back to local execution (#283)."""
    _daemon_dead(monkeypatch, "daemon response timed out")
    args = {"q": "x"}
    assert server._daemon_call("search", args, 5) is None
    assert args["_daemon_unavailable"] == "daemon response timed out"


def test_search_falls_back_locally_when_daemon_dead(wired, monkeypatch):
    """#283: a dead daemon must not turn search into an _error retry loop —
    the local read path answers instead and says so."""
    c = _tree_crawl(wired, monkeypatch)
    _daemon_dead(monkeypatch)
    res = server.search(c["scope_handle"], "text")
    assert res["evidence"] and "_error" not in res
    assert "daemon" in res and "answered locally" in res["daemon"]
    assert "spawn failed" in res["daemon"]
    assert res["timings"]["retrieval_ms"] >= 0


def test_search_falls_back_on_daemon_timeout(wired, monkeypatch):
    """Alive-but-slow daemon: rpc.wait times out → same local fallback."""
    from confluence_knowledge.rpc import DaemonUnavailable
    c = _tree_crawl(wired, monkeypatch)
    monkeypatch.setenv("CONF_KNOWLEDGE_DAEMON", "1")
    monkeypatch.setattr(server, "_daemon_on", lambda: True)

    class _SlowRpc:
        def submit(self, *a, **k):
            return "rid1"
        def wait(self, rid, timeout_s, on_tick=None):
            raise DaemonUnavailable("daemon response timed out")
    monkeypatch.setattr(server, "_rpc", lambda: _SlowRpc())
    monkeypatch.setattr(server, "ensure_daemon", lambda rpc: None)
    res = server.search(c["scope_handle"], "text")
    assert res["evidence"] and "_error" not in res
    assert "timed out" in res["daemon"]


def test_verify_citations_falls_back_locally(wired, monkeypatch):
    """Evidence set lives in the proxy after a local-fallback search —
    verify must check it locally rather than erroring."""
    c = _tree_crawl(wired, monkeypatch)
    _daemon_dead(monkeypatch)
    res = server.search(c["scope_handle"], "text")
    q = res["evidence"][0]
    ok = server.verify_citations(c["scope_handle"], res["evidence_set"], [
        {"claim": "x", "chunk_id": q["chunk_id"],
         "quote": q["text"][:12]}])
    assert ok["ok"] is True and "daemon" in ok


def test_start_crawl_still_refuses_locally_with_reason(wired, monkeypatch):
    """Writes never bypass the daemon (#189) — the guard still fires,
    now carrying the concrete cause."""
    c = _tree_crawl(wired, monkeypatch)
    _daemon_dead(monkeypatch, "daemon response timed out")
    out = server.start_crawl(c["scope_handle"])
    assert out["_error"] and out["daemon"] == "starting"
    assert "timed out" in out["reason"]


def test_search_reports_timings(wired, monkeypatch):
    """Self-diagnosing latency: every search carries phase timings."""
    c = _tree_crawl(wired, monkeypatch)
    res = server.search(c["scope_handle"], "text")
    assert res["timings"]["retrieval_ms"] >= 0
    assert res["timings"]["search_ms"] >= res["timings"]["retrieval_ms"]


def test_status_reclaims_orphaned_crawl(wired, monkeypatch):
    """#285: local mode — a 'running' job whose worker died with its
    process is reclaimed by the first status/connect call (the daemon's
    boot sweep moved into the server)."""
    c = server.connect("1")
    job = server.start_crawl(c["scope_handle"])
    key = c["scope_handle"].split(":")[1]
    server._workers.clear()                     # worker died with process
    reclaimed = []
    monkeypatch.setattr(server, "_resume_job",
                        lambda k, j: reclaimed.append((k, j)))
    monkeypatch.setattr(server, "_ORPHANS_SWEPT", False)
    server.status()
    assert (key, job["job_id"]) in reclaimed
    # once-guard: a second status must not re-sweep
    reclaimed.clear()
    server.status()
    assert reclaimed == []


def test_orphan_sweep_skipped_under_daemon(wired, monkeypatch):
    """#285: daemon mode keeps the daemon's own boot sweep — the
    server-side sweep must not also fire (double workers)."""
    monkeypatch.setattr(server, "_daemon_on", lambda: True)
    called = []
    monkeypatch.setattr(server, "_resume_job",
                        lambda k, j: called.append(j))
    monkeypatch.setattr(server, "_ORPHANS_SWEPT", False)
    server._resume_orphaned_crawls()
    assert called == [] and server._ORPHANS_SWEPT is False


def test_status_reports_no_daemon_url_locally(wired, monkeypatch):
    """#285: local mode must not advertise a daemon endpoint."""
    monkeypatch.setattr(server, "_daemon_url",
                        lambda: "http://127.0.0.1:1/")
    out = server.status()
    assert out["daemon"] == "off" and out["paths"]["daemon_url"] == ""
