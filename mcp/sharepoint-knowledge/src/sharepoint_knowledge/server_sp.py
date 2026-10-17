"""stdio MCP server for sharepoint-knowledge — the only surface the agent
reaches. Scope = configured site + signed-in account + chosen subtree."""

from __future__ import annotations

import json
import logging
import threading

from mcp.server.fastmcp import FastMCP

from confluence_knowledge.client import AccessError, SecurityException
from confluence_knowledge.index import Index
from confluence_knowledge.retrieval import (
    MODEL_MARKER as EMBED_MODEL_ID,
)
from confluence_knowledge.retrieval import (
    LocalEmbedder,
    LocalReranker,
    LocalSparseEmbedder,
    check_citations,
    retrieve,
)
from confluence_knowledge.settings import SetupError

from .auth_sp import acquire_token
from .client_sp import download_file, make_client
from .crawl_sp import SPCrawler, resolve_root
from .settings_sp import APP_NAME, SPProfile, graph_base, home, index_root, workdir

mcp = FastMCP("sharepoint-knowledge")

_scopes: dict[str, dict] = {}
_evidence_sets: dict[str, list[dict]] = {}
_indexes: dict[str, Index] = {}
_clients: dict[str, object] = {}
_EMBEDDER = None
_RERANKER = None
_SPARSE = None
_model_lock = threading.Lock()
_models_ready = threading.Event()
_warm_lock = threading.Lock()
_warm_started = False

LOADING_NOTE = "loading local models — call this tool again"


def _index_for(scope_key: str) -> Index:
    idx = _indexes.get(scope_key)
    if idx is None:
        idx = _indexes[scope_key] = Index(index_root() / f"{scope_key}.db")
    return idx


def _embedder():
    global _EMBEDDER
    with _model_lock:
        if _EMBEDDER is None:
            _EMBEDDER = LocalEmbedder(home(), APP_NAME)
    return _EMBEDDER


def _reranker():
    global _RERANKER
    with _model_lock:
        if _RERANKER is None:
            _RERANKER = LocalReranker(home(), APP_NAME)
    return _RERANKER


def _sparse():
    global _SPARSE
    with _model_lock:
        if _SPARSE is None:
            _SPARSE = LocalSparseEmbedder(home(), APP_NAME)
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
    threading.Thread(target=_warm_models, name="model-warmup", daemon=True).start()


def _models_loaded(wait: float) -> bool:
    _start_warmup()
    return _models_ready.wait(wait)


def _client_for(account: str) -> object:
    cli = _clients.get(account)
    if cli is None:
        profile = SPProfile.load()
        token = acquire_token(profile, workdir_from_home(), home())
        cli = _clients[account] = make_client(graph_base(), token)
    return cli


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
        "models_loaded": _models_ready.is_set() and _EMBEDDER is not None,
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
    principal = "me"
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
    ev = retrieve(idx, question, _embedder(), _reranker(), sparse=_sparse())
    set_id = f"ev:{len(_evidence_sets)}:{s['key'][:8]}"
    _evidence_sets[set_id] = ev
    return {
        "evidence_set": set_id,
        "coverage": _coverage(idx),
        "as_of": st["as_of"],
        "evidence": [
            {"chunk_id": e["chunk_id"], "page_title": e["page_title"],
             "page_url": e["page_url"], "heading": e["heading"],
             "score": e.get("rerank_score"), "text": e["text"][:2000]}
            for e in ev
        ],
    }


@mcp.tool()
def get_evidence(scope_handle: str, chunk_ids: list[str]) -> dict:
    """Return full text of previously returned chunks — same scope only."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    all_chunks = {c["chunk_id"]: c for c in idx.active_chunks()}
    out = []
    for cid in chunk_ids[:16]:
        c = all_chunks.get(cid)
        if c is None:
            raise SecurityException(f"chunk {cid} not in this scope's index")
        p = idx.active_pages().get(c["page_id"], {})
        out.append({"chunk_id": cid, "text": c["text"],
                    "page_title": p.get("title", ""), "page_url": p.get("url", "")})
    return {"chunks": out}


@mcp.tool()
def verify_citations(scope_handle: str, evidence_set: str,
                     mappings: list[dict]) -> dict:
    _scope(scope_handle)
    ev = _evidence_sets.get(evidence_set)
    if ev is None or not evidence_set.endswith(_scopes[scope_handle]["key"][:8]):
        raise SecurityException(
            "unknown or cross-scope evidence set — run search again and cite its evidence_set")
    return check_citations(ev, mappings)


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _start_warmup()
    mcp.run()


if __name__ == "__main__":
    main()
