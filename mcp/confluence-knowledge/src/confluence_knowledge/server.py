"""stdio MCP server — the only surface the Copilot agent can reach.

Exposes narrow operations; no arbitrary HTTP/shell/file tools. Scope handles
are opaque and bound to the site+account+root partition.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .auth import get_credential
from .client import AccessError, ConfluenceClient, SecurityException, make_client
from .crawl import Crawler
from .index import Index
from .retrieval import (
    MODEL_MARKER as EMBED_MODEL_ID,
)
from .retrieval import (
    LocalEmbedder,
    LocalReranker,
    LocalSparseEmbedder,
    check_citations,
    retrieve,
)
from .settings import (
    SetupError,
    SiteProfile,
    default_home,
    extract_page_id,
    resolve_workdir,
    workspace_index_root,
)

mcp = FastMCP("confluence-knowledge")

# in-process caches (per server lifetime)
_scopes: dict[str, dict] = {}
_evidence_sets: dict[str, list[dict]] = {}
_indexes: dict[str, Index] = {}  # one connection per DB for the server lifetime
_clients: dict[str, ConfluenceClient] = {}  # one HTTP pool per credential
_EMBEDDER = None
_RERANKER = None
_SPARSE = None
_model_lock = threading.Lock()
_models_ready = threading.Event()
_warm_lock = threading.Lock()
_warm_started = False

LOADING_NOTE = "loading local models — call this tool again"


def _home() -> Path:
    return default_home()


def _workdir() -> Path:
    return resolve_workdir()


def _step_seconds() -> float:
    """Upper bound for one tool call, kept well under IDE request timeouts."""
    return float(os.environ.get("CONF_KNOWLEDGE_STEP_SECONDS", "10"))


def _client_for(profile: SiteProfile):
    c = _clients.get(profile.credential_ref)
    if c is None:
        token = get_credential(_workdir(), profile.credential_ref)
        if not token:
            raise SetupError(
                "no stored token — run `confluence-knowledge setup` to configure credentials")
        c = _clients[profile.credential_ref] = make_client(profile, token)
    return c


def _index_for(scope_key: str) -> Index:
    idx = _indexes.get(scope_key)
    if idx is None:
        idx = _indexes[scope_key] = Index(workspace_index_root(_workdir()) / f"{scope_key}.db")
    return idx


def _embedder():
    global _EMBEDDER
    with _model_lock:
        if _EMBEDDER is None:
            _EMBEDDER = LocalEmbedder(_home())
    return _EMBEDDER


def _reranker():
    global _RERANKER
    with _model_lock:
        if _RERANKER is None:
            _RERANKER = LocalReranker(_home())
    return _RERANKER


def _sparse():
    global _SPARSE
    with _model_lock:
        if _SPARSE is None:
            _SPARSE = LocalSparseEmbedder(_home())
    return _SPARSE


def _warm_models() -> None:
    try:
        _embedder()
        _reranker()
        _sparse()
    except SetupError:
        pass  # reported by the tool that needs the model
    finally:
        _models_ready.set()


def _start_warmup() -> None:
    """Load the ONNX sessions off the request path; the first load can take
    longer than an IDE's request timeout."""
    global _warm_started
    with _warm_lock:
        if _warm_started:
            return
        _warm_started = True
    threading.Thread(target=_warm_models, name="model-warmup", daemon=True).start()


def _models_loaded(wait: float) -> bool:
    _start_warmup()
    return _models_ready.wait(wait)


def _scope(scope_handle: str) -> dict:
    s = _scopes.get(scope_handle)
    if s is None:
        # the IDE may restart the server (e.g. after a request timeout) and
        # drop in-memory scopes; handles carry the root so they re-bind after
        # the same identity check connect() performs
        parts = scope_handle.split(":")
        if len(parts) == 3 and parts[0] == "scope" and connect(parts[2])["scope_handle"] == scope_handle:
            return _scopes[scope_handle]
        raise SetupError("unknown scope — call connect first")
    return s


@mcp.tool()
def status() -> dict:
    """Report setup readiness — never returns secrets."""
    home = _home()
    try:
        profile = SiteProfile.load(home)
        cfg = {"env": profile.env, "site": profile.base_url}
    except SetupError as e:
        cfg = {"error": str(e)}
    from .health import health_checks
    from .registration import SERVER_ID
    idx_root = workspace_index_root(_workdir())
    checks = health_checks(home, _workdir(), SERVER_ID, "confluence-knowledge",
                           index_root=idx_root)
    if "error" not in cfg:
        # cheap liveness: a 401 here means the saved token died — point at
        # setup, not at the docs
        try:
            client = _client_for(profile)
            ep = ("wiki/rest/api/user/current" if profile.env == "cloud"
                  else "rest/api/user/current")
            client.get(ep)
            ok, detail = True, "token valid"
        except (AccessError, SetupError) as e:
            ok, detail = False, str(e)
        checks.append({"flag": "creds_valid", "ok": ok, "detail": detail,
                       "remediation": None if ok else
                       "confluence-knowledge setup (rotate the token)"})
    return {
        "configured": "error" not in cfg,
        "profile": cfg,
        "checks": checks,
        "models_loaded": _models_ready.is_set() and _EMBEDDER is not None,
        "index_root": str(idx_root),
        "live_scopes": sorted(_scopes),
    }


@mcp.tool()
def connect(root: str) -> dict:
    """Validate credentials + root readability; bind a scope handle."""
    profile = SiteProfile.load(_home())
    root_id = extract_page_id(root)
    _clients.pop(profile.credential_ref, None)  # re-read the token: setup may have rotated it
    client = _client_for(profile)
    who = client.get("wiki/rest/api/user/current" if profile.env == "cloud"
                     else "rest/api/user/current").json()
    principal = who.get("accountId") or who.get("userKey") or who.get("username") or "?"
    scope_key = profile.scope_key(principal, root_id)
    idx = _index_for(scope_key)
    st = idx.stats()
    handle = f"scope:{scope_key}:{root_id}"
    _scopes[handle] = {"key": scope_key, "profile": profile, "root": root_id}
    _start_warmup()
    stale = idx.model_id() is not None and idx.model_id() != EMBED_MODEL_ID
    return {
        "scope_handle": handle,
        "root_page_id": root_id,
        "principal": principal,
        "cached": st["pages"] > 0,
        "cached_pages": st["pages"],
        "cached_as_of": st["as_of"],
        "stale_model": stale,
        "note": ("index was built with an older model — run start_crawl once "
                 "to rebuild it with the current models" if stale else None),
    }


def _crawler(s: dict, embedder=None, sparse=None) -> Crawler:
    return Crawler(_client_for(s["profile"]), _index_for(s["key"]), s["profile"].env,
                   embedder=embedder, sparse=sparse,
                   site_url=s["profile"].base_url)


# background crawl workers — a crawl runs to completion server-side so a big
# subtree doesn't need hundreds of tool calls (each one an IDE LLM turn and a
# timeout surface, #81). crawl_step is then only a progress read.
_workers: dict[str, threading.Thread] = {}
_worker_errors: dict[str, str] = {}
_workers_lock = threading.Lock()


def _worker_loop(s: dict, job_id: str) -> None:
    idx = _index_for(s["key"])
    try:
        # models may still be warming; the worker waits, not the tool call
        _models_ready.wait()
        c = _crawler(s, embedder=_embedder(), sparse=_sparse())
        while True:
            r = c.step(job_id, batch=None, budget_s=None)
            if r["state"] != "running":
                break
    except Exception as e:  # noqa: BLE001 — surfaced via job state
        err = f"{type(e).__name__}: {e}"
        _worker_errors[f"{s['key']}:{job_id}"] = err
        job = idx.job(job_id)
        if job and job["state"] == "running":
            idx.save_job(job_id, "error", job["queue"], job["errors"] + [err])


def _ensure_worker(s: dict, job_id: str) -> None:
    """Start the crawl worker if none is alive — also the resume path after
    the server restarts mid-crawl (the queue is durable, the thread isn't)."""
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
    """Begin crawling the root page and all descendants in a background
    thread. The crawl runs to completion without further input — call
    crawl_step only to read progress."""
    s = _scope(scope_handle)
    job_id = _crawler(s).start(s["root"])
    _ensure_worker(s, job_id)
    return {"job_id": job_id, "state": "running",
            "note": "crawl runs in the background — call crawl_step to poll"}


@mcp.tool()
def crawl_step(scope_handle: str, job_id: str) -> dict:
    """Read crawl progress instantly. A running job with no live worker (e.g.
    the server restarted mid-crawl) is resumed automatically; the queue and
    per-page checkpoints are durable."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    job = idx.job(job_id)
    if job is None:
        raise AccessError(f"unknown job {job_id}")
    if job["state"] == "running":
        if not _models_ready.is_set():
            _start_warmup()
            note = LOADING_NOTE
        else:
            note = None
        _ensure_worker(s, job_id)
        p = idx.job_progress(job_id)
        if note:
            p["note"] = note
        return p
    p = idx.job_progress(job_id)
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
                "note": "no pages indexed — run a crawl first"}
    if not _models_loaded(_step_seconds()):
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
    # httpx logs every request URL at INFO; keep the IDE's MCP log readable
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _start_warmup()
    mcp.run()


if __name__ == "__main__":
    main()
