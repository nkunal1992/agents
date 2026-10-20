"""stdio MCP server for sharepoint-knowledge — the only surface the agent
reaches. Scope = configured site + signed-in account + chosen subtree."""

from __future__ import annotations

import json
import logging
import threading
import uuid

from confluence_knowledge.client import AccessError, SecurityException
from confluence_knowledge.feedback import FeedbackStore
from confluence_knowledge.index import Index
from confluence_knowledge.models import ModelPool
from confluence_knowledge.resources import lower_priority
from confluence_knowledge.retrieval import (
    MODEL_MARKER as EMBED_MODEL_ID,
)
from confluence_knowledge.retrieval import (
    check_citations,
    retrieve,
)
from confluence_knowledge.settings import SetupError
from mcp.server.fastmcp import FastMCP

from .auth_sp import acquire_token
from .client_sp import download_file, make_client
from .crawl_sp import SPCrawler, resolve_root
from .settings_sp import APP_NAME, SPProfile, graph_base, home, index_root, workdir

mcp = FastMCP("sharepoint-knowledge")

_scopes: dict[str, dict] = {}
_evidence_sets: dict[str, dict] = {}  # set_id → {scope, ev}
_indexes: dict[str, Index] = {}
_feedback: dict[str, FeedbackStore] = {}
_clients: dict[str, object] = {}
# module globals kept for test injection; the pool owns real model objects
_EMBEDDER = None
_RERANKER = None
_SPARSE = None
_models_ready = threading.Event()
_warm_lock = threading.Lock()
_warm_started = False

LOADING_NOTE = "loading local models — call this tool again"
EVIDENCE_CHAR_CAP = 12000  # payload cap; `truncated` flags longer chunks


def _index_for(scope_key: str) -> Index:
    idx = _indexes.get(scope_key)
    if idx is None:
        idx = _indexes[scope_key] = Index(index_root() / f"{scope_key}.db")
    return idx

def _feedback_for(scope_key: str) -> FeedbackStore:
    st = _feedback.get(scope_key)
    if st is None:
        st = _feedback[scope_key] = FeedbackStore(
            index_root(), scope_key, "sharepoint-knowledge")
    return st


def _any_worker_alive() -> bool:
    return any(t.is_alive() for t in list(_workers.values()))


def _on_models_unloaded() -> None:
    global _EMBEDDER, _RERANKER, _SPARSE, _warm_started
    _EMBEDDER = _RERANKER = _SPARSE = None
    with _warm_lock:
        _warm_started = False
    _models_ready.clear()
    for idx in _indexes.values():
        idx.drop_search_cache()


_pool = ModelPool(home, APP_NAME, busy_fn=_any_worker_alive,
                  on_unload=_on_models_unloaded,
                  ready_event=_models_ready)


def _embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = _pool.embedder()
    return _EMBEDDER


def _reranker():
    global _RERANKER
    if _RERANKER is None:
        _RERANKER = _pool.reranker()
    return _RERANKER


def _sparse():
    global _SPARSE
    if _SPARSE is None:
        _SPARSE = _pool.sparse()
    return _SPARSE


def _warm_models() -> None:
    try:
        _embedder()
        _reranker()
        _sparse()
    except SetupError:
        pass
    finally:
        _models_ready.set()


def _start_warmup() -> None:
    global _warm_started
    with _warm_lock:
        if _warm_started:
            return
        _warm_started = True
    threading.Thread(target=_warm_models, name="model-warmup",
                     daemon=True).start()


def _models_loaded(wait: float) -> bool:
    _start_warmup()
    return _models_ready.wait(wait)


def _client_for(account: str) -> object:
    """Cached per account — but a fresh token replaces the cached client
    rather than leaving it on a stale credential (R06). Entries are
    (token, client) pairs."""
    profile = SPProfile.load()
    token = acquire_token(profile, workdir_from_home(), home())
    entry = _clients.get(account)
    if entry is None or entry[0] != token:
        entry = (token, make_client(graph_base(), token))
        _clients[account] = entry
    return entry[1]


def workdir_from_home():
    from .settings_sp import workdir
    return workdir()


def _scope(scope_handle: str) -> dict:
    s = _scopes.get(scope_handle)
    if s is None:
        parts = scope_handle.split(":", 3)
        if len(parts) == 4 and parts[0] == "scope" and \
                connect(parts[3])["scope_handle"] == scope_handle:
            return _scopes[scope_handle]
        raise SetupError("unknown scope — call connect first")
    return s


@mcp.tool()
def status() -> dict:
    """Report setup readiness — profile present, token reachable, models."""
    try:
        profile = SPProfile.load()
        try:
            token = acquire_token(profile, workdir_from_home(), home())
            who = "token ready"
        except SetupError as e:
            who = f"sign-in needed: {e}"
        cfg = {"site": profile.site_url, "tenant": profile.tenant_id,
               "auth": who}
        configured = who == "token ready"
    except SetupError as e:
        cfg = {"error": str(e)}
        configured = False
        token = None
    if token:
        try:
            cli = _client_for(profile.site_url)
            me = cli.get("me").json()
            cfg["signed_in_as"] = me.get("userPrincipalName") or me.get("mail")
        except AccessError as e:
            cfg["auth"] = str(e)
            configured = False
    from confluence_knowledge.health import health_checks
    checks = health_checks(home(), workdir(), APP_NAME, "sharepoint-knowledge",
                           index_root=index_root())
    checks.append({
        "flag": "creds_valid", "ok": configured,
        "detail": cfg.get("auth") if configured
                  else cfg.get("auth") or "no profile",
        "remediation": None if configured
                       else "sharepoint-knowledge setup (re-sign-in)"})
    return {
        "configured": configured,
        "profile": cfg,
        "checks": checks,
        "models_loaded": _models_ready.is_set() and
        (_EMBEDDER is not None or _pool.is_loaded()),
        "index_root": str(index_root()),
        "live_scopes": sorted(_scopes),
    }


@mcp.tool()
def connect(root: str) -> dict:
    """Bind a scope to a SharePoint URL — a site (pages + all libraries),
    or a library/folder path inside it. One root per connect."""
    profile = SPProfile.load()
    cli = _client_for(profile.site_url)
    if root.startswith(("site:", "item:", "drive:", "list:", "page:")):
        root_entry, site_web = root, profile.site_url
    else:
        root_entry, site_web = resolve_root(cli, root)
    # Bind the scope to the real account — /me has no identity under an
    # app-only token, so fall back to the app's client_id there (R06).
    try:
        me = cli.get("me").json()
        principal = me.get("id") or me.get("userPrincipalName") \
            or profile.client_id
    except AccessError:
        principal = profile.client_id
    scope_key = profile.scope_key(principal, root_entry)
    idx = _index_for(scope_key)
    st = idx.stats()
    handle = f"scope:{scope_key}:{root}"
    _scopes[handle] = {"key": scope_key, "root_entry": root_entry,
                       "site_web": site_web, "client": cli}
    _start_warmup()
    stale = idx.model_id() is not None and idx.model_id() != EMBED_MODEL_ID
    return {
        "scope_handle": handle,
        "root": root_entry,
        "site": site_web,
        "cached": st["pages"] > 0,
        "cached_pages": st["pages"],
        "cached_as_of": st["as_of"],
        "stale_model": stale,
        "note": ("index was built with an older model — run start_crawl once "
                 "to rebuild it with the current models" if stale else None),
    }


_workers: dict[str, threading.Thread] = {}
_worker_errors: dict[str, str] = {}
_workers_lock = threading.Lock()


def _worker_loop(s: dict, job_id: str) -> None:
    idx = _index_for(s["key"])
    try:
        _start_warmup()
        _models_ready.wait()
        c = SPCrawler(s["client"], idx, embedder=_embedder(),
                      downloader=download_file, sparse=_sparse())
        r = c.step(job_id)
        while r["state"] == "running":
            r = c.step(job_id)
    except Exception as e:  # noqa: BLE001 — surfaced via job state
        err = f"{type(e).__name__}: {e}"
        _worker_errors[f"{s['key']}:{job_id}"] = err
        job = idx.job(job_id)
        if job and job["state"] == "running":
            idx.save_job(job_id, "error", job["queue"], job["errors"] + [err])


def _ensure_worker(s: dict, job_id: str) -> None:
    key = f"{s['key']}:{job_id}"
    with _workers_lock:
        t = _workers.get(key)
        if t is not None and t.is_alive():
            return
        _workers[key] = threading.Thread(
            target=_worker_loop, args=(s, job_id),
            name=f"crawl-{job_id}", daemon=True)
        _workers[key].start()


@mcp.tool()
def start_crawl(scope_handle: str, refresh: bool = False) -> dict:
    """Begin indexing the root in the background (Site Pages + libraries)."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    c = SPCrawler(s["client"], idx)
    job_id = c.start(s["root_entry"])
    _ensure_worker(s, job_id)
    return {"job_id": job_id, "state": "running",
            "note": "crawl runs in the background — call crawl_step to poll"}


@mcp.tool()
def crawl_step(scope_handle: str, job_id: str) -> dict:
    """Read crawl progress instantly; resumes a dead worker automatically."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    job = idx.job(job_id)
    if job is None:
        raise AccessError(f"unknown job {job_id}")

    def progress() -> dict:
        p = idx.job_progress(job_id)
        warns = idx.meta_get(f"warns:{job_id}")
        if warns:
            p["warnings"] = json.loads(warns)
        return p

    if job["state"] == "running":
        if not _models_ready.is_set():
            _start_warmup()
            note = LOADING_NOTE
        else:
            note = None
        _ensure_worker(s, job_id)
        p = progress()
        if note:
            p["note"] = note
        return p
    p = progress()
    if p["state"] == "done":
        p["note"] = "no further calls needed — the index is published"
    return p


def _coverage(idx: Index) -> str:
    job = idx.latest_job()
    if job is None or job["state"] == "abandoned":
        return "unknown"
    if job["state"] == "running":
        return "refresh in progress — answering from the previous snapshot"
    return "partial" if job["errors"] else "complete"


@mcp.tool()
def search(scope_handle: str, question: str) -> dict:
    """Hybrid retrieve + local rerank; returns bounded, cited evidence."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    st = idx.stats()
    if st["pages"] == 0:
        job = idx.latest_job()
        if job and job["state"] == "running":
            return {"evidence": [], "coverage": "crawling",
                    "note": f"crawl {job['job_id']} still running — keep calling crawl_step "
                            "until state is done, then search"}
        return {"evidence": [], "coverage": "empty",
                "note": "nothing indexed — run a crawl first"}
    if not _models_loaded(10.0):
        return {"evidence": [], "coverage": "loading", "note": LOADING_NOTE}
    if idx.model_id() is not None and idx.model_id() != EMBED_MODEL_ID:
        return {"evidence": [], "coverage": "stale_model",
                "note": "this index was built with an older embedding model "
                        "— run start_crawl once to rebuild it, then search again"}
    emb = _embedder()
    ev = retrieve(idx, question, emb, _reranker(), sparse=_sparse())
    corrections = _feedback_for(s["key"]).match(emb.embed([question])[0])
    # unique immutable handle — never derived from cache size; bound to the
    # snapshot epoch so a refresh can't silently re-point a citation
    set_id = f"ev:{s['key'][:8]}:{uuid.uuid4().hex[:16]}"
    # bound the session cache — sets are never reused after the turn ends
    while len(_evidence_sets) >= 32:
        _evidence_sets.pop(next(iter(_evidence_sets)))
    _evidence_sets[set_id] = {"scope": s["key"], "ev": ev,
                              "epoch": idx.snapshot_epoch()}
    return {
        "evidence_set": set_id,
        "coverage": _coverage(idx),
        "as_of": st["as_of"],
        "user_corrections": corrections,
        "evidence": [
            {"chunk_id": e["chunk_id"], "page_id": e["page_id"],
             "page_title": e["page_title"], "page_url": e["page_url"],
             "page_version": e.get("page_version"),
             "heading": e["heading"], "kind": e["kind"],
             "ordinal": e["ordinal"],
             "score": e.get("rerank_score"), "text": e["text"][:EVIDENCE_CHAR_CAP],
             "low_confidence": e.get("low_confidence", False),
             "truncated": len(e["text"]) > EVIDENCE_CHAR_CAP}
            for e in ev
        ],
    }


@mcp.tool()
def get_evidence(scope_handle: str, chunk_ids: list[str] | None = None,
                 page_id: str | None = None) -> dict:
    """Return full text of chunks — same scope only. Pass `chunk_ids` for
    previously returned chunks, or `page_id` to list that page's chunks in
    ordinal order (Failure Hunt boundary probe: first/last chunks)."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    pages = idx.active_pages()
    out = []
    if page_id is not None:
        if page_id not in pages:
            raise SecurityException(f"page {page_id} not in this scope's index")
        out = idx.page_chunks(page_id)
    else:
        wanted = (chunk_ids or [])[:16]
        by_id = {c["chunk_id"]: c for c in idx.chunks_by_ids(wanted)}
        for cid in wanted:
            c = by_id.get(cid)
            if c is None:
                raise SecurityException(
                    f"chunk {cid} not in this scope's index")
            out.append(c)
    return {"chunks": [
        {"chunk_id": c["chunk_id"], "page_id": c["page_id"],
         "ordinal": c["ordinal"], "kind": c["kind"], "text": c["text"],
         "page_title": pages.get(c["page_id"], {}).get("title", ""),
         "page_url": pages.get(c["page_id"], {}).get("url", ""),
         "page_version": pages.get(c["page_id"], {}).get("version")
         or pages.get(c["page_id"], {}).get("fetched_at")}
        for c in out]}


@mcp.tool()


@mcp.tool()
def submit_feedback(scope_handle: str, question: str, correction: str,
                    note: str = "") -> dict:
    """Store a saved correction — disclosed alongside the docs on
    similar future questions (never overrides them)."""
    s = _scope(scope_handle)
    if not _models_loaded(0):
        return {"stored": False, "note": LOADING_NOTE}
    qvec = _embedder().embed([question])[0]
    entry = _feedback_for(s["key"]).add(question, qvec, correction, note)
    return {"stored": entry["id"],
            "note": "will be disclosed on similar questions"}


@mcp.tool()
def feedback_report(scope_handle: str) -> dict:
    """Aggregated corrections — the agent infers which doc keeps
    getting corrected and suggests it to the user."""
    s = _scope(scope_handle)
    return _feedback_for(s["key"]).report()

@mcp.tool()
def verify_citations(scope_handle: str, evidence_set: str,
                     mappings: list[dict]) -> dict:
    s = _scope(scope_handle)
    rec = _evidence_sets.get(evidence_set)
    if rec is None or rec["scope"] != s["key"]:
        raise SecurityException(
            "unknown or cross-scope evidence set — run search again and cite its evidence_set")
    idx = _index_for(s["key"])
    if rec.get("epoch") != idx.snapshot_epoch():
        raise SecurityException(
            "evidence set predates the latest published snapshot — "
            "run search again and cite the fresh evidence_set")
    return check_citations(rec["ev"], mappings)


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # lowered priority, no eager model load, idle unload — see
    # confluence_knowledge.resources / .models for the policy
    lower_priority()
    _pool.start_reaper()
    mcp.run()


if __name__ == "__main__":
    main()
