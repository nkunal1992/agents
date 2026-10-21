"""stdio MCP server for sharepoint-knowledge — the only surface the agent
reaches. Scope = configured site + signed-in account + chosen subtree."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid

from mcp.server.fastmcp import FastMCP

from confluence_knowledge.client import AccessError, SecurityException
from confluence_knowledge.feedback import FeedbackStore
from confluence_knowledge.index import Index
from confluence_knowledge.jobs import (
    CrawlCancelled,
    LeaseOwner,
    Progress,
    crawl_control,
)
from confluence_knowledge.mcp_async import tool
from confluence_knowledge.models import ModelPool
from confluence_knowledge.notify import notify
from confluence_knowledge.resources import lower_priority
from confluence_knowledge.retrieval import (
    MODEL_MARKER as EMBED_MODEL_ID,
)
from confluence_knowledge.retrieval import (
    check_citations,
    retrieve,
)
from confluence_knowledge.settings import SetupError

from .auth_sp import acquire_token
from .client_sp import download_file, make_client
from .crawl_sp import SPCrawler, resolve_root
from .settings_sp import APP_NAME, SPProfile, graph_base, home, index_root, workdir

mcp = FastMCP("sharepoint-knowledge")

# tools run on worker threads now (mcp_async) — one lock guards the caches
_state_lock = threading.RLock()
_scopes: dict[str, dict] = {}
_evidence_sets: dict[str, dict] = {}  # set_id → {scope, ev}
_indexes: dict[str, Index] = {}
_feedback: dict[str, FeedbackStore] = {}
_clients: dict[str, object] = {}
_owner = LeaseOwner()   # process identity for cross-process crawl leases
_progress: dict[str, Progress] = {}  # job_id → live progress (lock-free reads)
_cancel_flags: dict[str, threading.Event] = {}  # job_id → cancel requested
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
        with _state_lock:
            idx = _indexes.get(scope_key)
            if idx is None:
                idx = _indexes[scope_key] = Index(index_root() / f"{scope_key}.db")
    return idx

def _feedback_for(scope_key: str) -> FeedbackStore:
    st = _feedback.get(scope_key)
    if st is None:
        with _state_lock:
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
        with _state_lock:
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


@tool(mcp)
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
    root = index_root()
    last = None
    try:
        cands = sorted(root.glob("*.db"), key=lambda p: -p.stat().st_mtime)
        if cands:
            last = _last_crawl(_index_for(cands[0].stem))
    except Exception:  # noqa: BLE001 — status must never fail on this
        last = None
    return {
        "configured": configured,
        "profile": cfg,
        "checks": checks,
        "models_loaded": _models_ready.is_set() and
        (_EMBEDDER is not None or _pool.is_loaded()),
        "index_root": str(root),
        "live_scopes": sorted(_scopes),
        "last_crawl": last,
    }


@tool(mcp)
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
    with _state_lock:
        _scopes[handle] = {"key": scope_key, "root_entry": root_entry,
                           "site_web": site_web, "client": cli}
    _start_warmup()
    stale = idx.model_id() is not None and idx.model_id() != EMBED_MODEL_ID
    auto = _maybe_auto_reindex(_scopes[handle], idx) if stale else None
    return {
        "scope_handle": handle,
        "root": root_entry,
        "site": site_web,
        "cached": st["pages"] > 0,
        "cached_pages": st["pages"],
        "cached_as_of": st["as_of"],
        "stale_model": stale,
        "auto_reindex": auto,
        "last_crawl": _last_crawl(idx),
        "note": ("index was built with an older model — "
                 + (f"re-index started automatically ({auto})"
                    if auto else "run start_crawl once to rebuild it "
                    "with the current models") if stale else None),
    }


_workers: dict[str, threading.Thread] = {}
_worker_errors: dict[str, str] = {}
_workers_lock = threading.Lock()


_notified: set[str] = set()   # job_ids already notified this process


def _notify_terminal(idx: Index, job_id: str) -> None:
    """One desktop notification per finished job — the crawl runs
    unattended, so completion/failure/cancel must not need a poll."""
    if job_id in _notified:
        return
    job = idx.job(job_id)
    if job is None or job["state"] == "running":
        return
    _notified.add(job_id)
    prog = _progress.get(job_id)
    n = prog.pages_done if prog is not None else 0
    label = {"done": "Crawl finished",
             "error": "Crawl failed",
             "cancelled": "Crawl cancelled",
             "abandoned": "Crawl stopped"}.get(job["state"], "Crawl ended")
    errs = job["errors"]
    body = (f"{n} items indexed" if job["state"] == "done"
            else f"{n} items seen")
    if errs:
        body += f" — {len(errs)} errors, last: {errs[-1][:60]}"
    notify(f"{label} — sharepoint-knowledge", body)


def _worker_loop(s: dict, job_id: str) -> None:
    idx = _index_for(s["key"])
    try:
        _start_warmup()
        _models_ready.wait()
        prog = _progress.setdefault(job_id, Progress())
        c = SPCrawler(s["client"], idx, embedder=_embedder(),
                      downloader=download_file, sparse=_sparse(),
                      control=crawl_control(
                          idx, job_id, prog,
                          _cancel_flags.setdefault(
                              job_id, threading.Event()),
                          _owner))
        r = c.step(job_id)
        while r["state"] == "running":
            r = c.step(job_id)
    except CrawlCancelled:
        pass  # terminal state already in the DB — nothing to overwrite
    except Exception as e:  # noqa: BLE001 — surfaced via job state
        err = f"{type(e).__name__}: {e}"
        _worker_errors[f"{s['key']}:{job_id}"] = err
        job = idx.job(job_id)
        if job and job["state"] == "running":
            idx.save_job(job_id, "error", job["queue"], job["errors"] + [err])
    finally:
        idx.release_lease(job_id, _owner.value)
        _notify_terminal(idx, job_id)


def _maybe_auto_reindex(s: dict, idx: Index) -> str | None:
    """#184 (parity #178): a stale-model index self-heals — notify once +
    start the rebuild crawl automatically unless a job is in flight."""
    latest = idx.latest_job()
    if latest and latest["state"] in ("running", "queued"):
        return None                               # already rebuilding
    if f"model:{s['key']}" not in _notified:
        _notified.add(f"model:{s['key']}")
        notify("sharepoint-knowledge",
               "index was built with an older embedding model — "
               "re-indexing so search stays compatible")
    if os.environ.get("SP_KNOWLEDGE_AUTO_REINDEX", "1") == "0":
        return None
    job_id = SPCrawler(s["client"], idx).start(s["root_entry"])
    _cancel_flags.pop(job_id, None)
    _progress[job_id] = Progress()
    return "started" if _ensure_worker(s, job_id) else "queued"


def _ensure_worker(s: dict, job_id: str) -> bool:
    """Start the worker unless another process holds the job's lease —
    exactly one worker per job across IDE windows."""
    key = f"{s['key']}:{job_id}"
    with _workers_lock:
        t = _workers.get(key)
        if t is not None and t.is_alive():
            return True
        idx = _index_for(s["key"])
        if not idx.acquire_lease(job_id, _owner.value, _owner.pid):
            prog = _progress.setdefault(job_id, Progress())
            prog.worker = "another IDE window"
            return False
        prog = _progress.setdefault(job_id, Progress())
        prog.worker = "self"
        _workers[key] = threading.Thread(
            target=_worker_loop, args=(s, job_id),
            name=f"crawl-{job_id}", daemon=True)
        _workers[key].start()
        return True


def _progress_for(idx: Index, job_id: str) -> dict:
    p = idx.job_progress(job_id)
    live = _progress.get(job_id)
    if live is not None:
        p.update(live.snapshot())
    else:
        blob = idx.meta_get(f"progress:{job_id}")
        other = Progress.from_blob(blob) if blob else None
        if other is not None:
            p.update(other.snapshot())
    warns = idx.meta_get(f"warns:{job_id}")
    if warns:
        p["warnings"] = json.loads(warns)
    # errors_n derives from the durable error list — a snapshot written
    # just before the last appends can lag; len(errors) is always true
    p["errors_n"] = max(p.get("errors_n", 0), len(p.get("errors", [])))
    return p


def _last_crawl(idx: Index) -> dict | None:
    """Latest job summary for status/connect — lets the agent answer
    'did the crawl finish?' without a second tool call."""
    job = idx.latest_job()
    if job is None:
        return None
    out = {"job_id": job["job_id"], "state": job["state"],
           "errors_n": len(job["errors"])}
    live = _progress.get(job["job_id"])
    blob = idx.meta_get(f"progress:{job['job_id']}")
    other = Progress.from_blob(blob) if blob else None
    snap = live.snapshot() if live is not None else \
        (other.snapshot() if other is not None else None)
    if snap:
        for k in ("pages_done", "phase", "rate_ppm", "eta_s", "worker"):
            if snap.get(k) is not None:
                out[k] = snap[k]
    return out


@tool(mcp)
def start_crawl(scope_handle: str, refresh: bool = False) -> dict:
    """Begin indexing the root in the background (Site Pages + libraries)."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    c = SPCrawler(s["client"], idx)
    job_id = c.start(s["root_entry"])
    _cancel_flags.pop(job_id, None)
    _progress[job_id] = Progress()
    if not _ensure_worker(s, job_id):
        return {"job_id": job_id, "state": "running",
                "worker": "another IDE window",
                "note": "crawl runs in another IDE window — crawl_step to poll"}
    return {"job_id": job_id, "state": "running",
            "note": "crawl runs in the background — call crawl_step to poll"}


@tool(mcp)
def crawl_step(scope_handle: str, job_id: str) -> dict:
    """Read crawl progress instantly; resumes a dead worker automatically."""
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
        p = _progress_for(idx, job_id)
        if note:
            p["note"] = note
        return p
    p = _progress_for(idx, job_id)
    if p["state"] == "done":
        p["note"] = "no further calls needed — the index is published"
    return p


@tool(mcp)
def wait_for_crawl(scope_handle: str, job_id: str,
                   wait_s: int = 50) -> dict:
    """Long-poll for a crawl change — returns as soon as the state or
    progress changes, or after `wait_s` (capped at 120; default fits IDE
    tool-call timeouts). The agent should prefer this over polling
    crawl_step in a tight loop. `changed` tells whether anything moved."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    job = idx.job(job_id)
    if job is None:
        raise AccessError(f"unknown job {job_id}")
    if job["state"] == "running":
        if not _models_ready.is_set():
            _start_warmup()
        _ensure_worker(s, job_id)
    wait_s = max(1, min(int(wait_s), 120))
    deadline = time.monotonic() + wait_s
    prev = _progress_for(idx, job_id)
    while prev["state"] == "running" and time.monotonic() < deadline:
        time.sleep(2)
        cur = _progress_for(idx, job_id)
        moved = (cur["state"] != prev["state"]
                 or cur.get("phase") != prev.get("phase")
                 or cur.get("pages_done") != prev.get("pages_done"))
        if moved:
            cur["changed"] = True
            return cur
        prev = cur
    prev["changed"] = False
    return prev


@tool(mcp)
def cancel_crawl(scope_handle: str, job_id: str) -> dict:
    """Stop a running crawl within seconds and mark the job cancelled — it
    never auto-resumes. The previously published snapshot is kept intact;
    start_crawl re-runs (unchanged items are reused)."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    job = idx.job(job_id)
    if job is None:
        raise AccessError(f"unknown job {job_id}")
    if job["state"] != "running":
        return {"job_id": job_id, "state": job["state"],
                "note": "already finished — nothing to cancel"}
    _cancel_flags.setdefault(job_id, threading.Event()).set()
    idx.save_job(job_id, "cancelled", job["queue"], job["errors"])
    prog = _progress.get(job_id)
    if prog is not None:
        prog.set_phase("cancelled")
    seen = idx.meta_get(f"seen:{job_id}")
    return {"job_id": job_id, "state": "cancelled",
            "pages_seen": len(json.loads(seen)) if seen else 0,
            "note": "previous snapshot kept — start_crawl to re-run "
                    "(unchanged items are reused)"}


def _coverage(idx: Index) -> str:
    job = idx.latest_job()
    if job is None or job["state"] == "abandoned":
        return "unknown"
    if job["state"] == "running":
        return "refresh in progress — answering from the previous snapshot"
    return "partial" if job["errors"] else "complete"


@tool(mcp, heavy=True)
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
    with _state_lock:
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


@tool(mcp)
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


@tool(mcp)
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


@tool(mcp)
def feedback_report(scope_handle: str) -> dict:
    """Aggregated corrections — the agent infers which doc keeps
    getting corrected and suggests it to the user."""
    s = _scope(scope_handle)
    return _feedback_for(s["key"]).report()

@tool(mcp)
def verify_citations(scope_handle: str, evidence_set: str,
                     mappings: list[dict]) -> dict:
    s = _scope(scope_handle)
    with _state_lock:
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
