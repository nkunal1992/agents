"""stdio MCP server for local-knowledge — the only surface the agent reaches.

Same tool surface as confluence-knowledge minus the credential machinery:
there is nothing to authenticate. Scope handles are opaque paths bound to the
canonical realpath partition.
"""

from __future__ import annotations

import logging
import socket
import threading

from confluence_knowledge.client import AccessError, SecurityException
from confluence_knowledge.feedback import FeedbackStore
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
from mcp.server.fastmcp import FastMCP

from .crawl_local import LocalCrawler, resolve_root
from .settings_local import APP_NAME, LocalProfile, home, index_root, workdir

mcp = FastMCP("local-knowledge")

_scopes: dict[str, dict] = {}
_evidence_sets: dict[str, dict] = {}  # set_id → {scope, ev}
_indexes: dict[str, Index] = {}
_feedback: dict[str, FeedbackStore] = {}
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

def _feedback_for(scope_key: str) -> FeedbackStore:
    st = _feedback.get(scope_key)
    if st is None:
        st = _feedback[scope_key] = FeedbackStore(
            index_root(), scope_key, "local-knowledge")
    return st


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


def _scope(scope_handle: str) -> dict:
    s = _scopes.get(scope_handle)
    if s is None:
        parts = scope_handle.split(":", 2)
        if len(parts) == 3 and parts[0] == "scope" and \
                connect(parts[2])["scope_handle"] == scope_handle:
            return _scopes[scope_handle]
        raise SetupError("unknown scope — call connect first")
    return s


@mcp.tool()
def status() -> dict:
    """Report setup readiness — no credentials exist for this agent."""
    try:
        profile = LocalProfile.load()
        cfg = {"root": profile.root_path, "device": profile.device}
    except SetupError as e:
        cfg = {"error": str(e)}
    from confluence_knowledge.health import health_checks
    return {
        "configured": "error" not in cfg,
        "profile": cfg,
        "checks": health_checks(home(), workdir(), APP_NAME, "local-knowledge",
                                index_root=index_root()),
        "models_loaded": _models_ready.is_set() and _EMBEDDER is not None,
        "index_root": str(index_root()),
        "live_scopes": sorted(_scopes),
    }


@mcp.tool()
def connect(root: str) -> dict:
    """Bind a scope to a file or folder path. No credentials needed — the
    filesystem itself is the ACL (we can only read what the user can)."""
    path = resolve_root(root)
    profile = LocalProfile(str(path), socket.gethostname())
    profile.save()          # remember last-used root for status/doctor
    scope_key = profile.scope_key()
    idx = _index_for(scope_key)
    st = idx.stats()
    handle = f"scope:{scope_key}:{path}"
    _scopes[handle] = {"key": scope_key, "root": path}
    _start_warmup()
    stale = idx.model_id() is not None and idx.model_id() != EMBED_MODEL_ID
    return {
        "scope_handle": handle,
        "root": str(path),
        "kind": "folder" if path.is_dir() else "file",
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
        c = LocalCrawler(idx, s["root"], embedder=_embedder(),
                         sparse=_sparse())
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
    """Begin indexing the root (file or folder, recursive) in the background."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    c = LocalCrawler(idx, s["root"])
    job_id = c.start()
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
        import json
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
                "note": "no files indexed — run a crawl first"}
    if not _models_loaded(10.0):
        return {"evidence": [], "coverage": "loading", "note": LOADING_NOTE}
    if idx.model_id() is not None and idx.model_id() != EMBED_MODEL_ID:
        return {"evidence": [], "coverage": "stale_model",
                "note": "this index was built with an older embedding model "
                        "— run start_crawl once to rebuild it, then search again"}
    emb = _embedder()
    ev = retrieve(idx, question, emb, _reranker(), sparse=_sparse())
    corrections = _feedback_for(s["key"]).match(emb.embed([question])[0])
    set_id = f"ev:{len(_evidence_sets)}:{s['key'][:8]}"
    # bound the session cache — sets are never reused after the turn ends
    while len(_evidence_sets) >= 32:
        _evidence_sets.pop(next(iter(_evidence_sets)))
    _evidence_sets[set_id] = {"scope": s["key"], "ev": ev}
    return {
        "evidence_set": set_id,
        "coverage": _coverage(idx),
        "as_of": st["as_of"],
        "user_corrections": corrections,
        "evidence": [
            {"chunk_id": e["chunk_id"], "page_title": e["page_title"],
             "page_url": e["page_url"], "heading": e["heading"],
             "score": e.get("rerank_score"), "text": e["text"][:12000],
             "low_confidence": e.get("low_confidence", False),
             "truncated": len(e["text"]) > 12000}
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


@mcp.tool()
def submit_feedback(scope_handle: str, question: str, correction: str,
                    note: str = "") -> dict:
    """Store a colleague correction — disclosed alongside the docs on
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
    """Aggregated corrections — which topics keep getting corrected points
    at outdated/confusing documentation."""
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
    return check_citations(rec["ev"], mappings)


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _start_warmup()
    mcp.run()


if __name__ == "__main__":
    main()
