"""stdio MCP server — the only surface the Copilot agent can reach.

Exposes narrow operations; no arbitrary HTTP/shell/file tools. Scope handles
are opaque and bound to the site+account+root partition.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

from .auth import get_credential
from .client import AccessError, ConfluenceClient, SecurityException, make_client
from .crawl import Crawler
from .feedback import FeedbackStore
from .index import Index, find_covering
from .jobs import CrawlCancelled, LeaseOwner, Progress, crawl_control
from .mcp_async import heartbeat, tool
from .models import shared_pool, shared_ready
from .notify import notify
from .reporting import audit, current_rss_mb, pulse, stages
from .resources import lower_priority
from .retrieval import (
    MODEL_MARKER as EMBED_MODEL_ID,
)
from .retrieval import (
    check_citations,
    fake_models,
    retrieve,
)
from .rpc import DaemonUnavailable, RpcDb, daemon_enabled, ensure_daemon
from .settings import (
    SetupError,
    SiteProfile,
    daemon_home,
    default_home,
    extract_page_id,
    resolve_workdir,
    workspace_index_root,
)

mcp = FastMCP("confluence-knowledge")

# in-process caches (per server lifetime) — tools run on worker threads
# now (mcp_async), so these are guarded by one lock
_state_lock = threading.RLock()
_CREDS_CACHE: dict[str, tuple[float, bool, str]] = {}
_CREDS_TTL_S = float(os.environ.get("CONF_KNOWLEDGE_CREDS_TTL_S", "120"))
_CREDS_REFRESHING: set[str] = set()


def _creds_check(profile) -> tuple[bool, str]:
    """One identity round-trip — kept off the `status` hot path (#173)."""
    try:
        client = _client_for(profile)
        ep = ("wiki/rest/api/user/current" if profile.env == "cloud"
              else "rest/api/user/current")
        client.get(ep)
        return True, "token valid"
    except (AccessError, SetupError) as e:
        return False, str(e)


def _creds_refresh_async(profile) -> None:
    """Refresh the cached verdict in a daemon thread — status returns the
    last-known result instantly and the next poll sees the fresh one."""
    ref = profile.credential_ref
    with _state_lock:
        if ref in _CREDS_REFRESHING:
            return
        _CREDS_REFRESHING.add(ref)

    def _run() -> None:
        ok, detail = _creds_check(profile)
        with _state_lock:
            _CREDS_CACHE[ref] = (time.time(), ok, detail)
            _CREDS_REFRESHING.discard(ref)

    threading.Thread(target=_run, daemon=True).start()
_scopes: dict[str, dict] = {}
_evidence_sets: dict[str, dict] = {}  # set_id → {scope, ev}
_indexes: dict[str, Index] = {}  # one connection per DB for the server lifetime
_clients: dict[str, ConfluenceClient] = {}  # one HTTP pool per credential
_feedback: dict[str, FeedbackStore] = {}  # per-scope correction stores
_owner = LeaseOwner()   # process identity for cross-process crawl leases
_progress: dict[str, Progress] = {}  # job_id → live progress (lock-free reads)
_cancel_flags: dict[str, threading.Event] = {}  # job_id → cancel requested
# module globals kept for test injection; the pool owns real model objects
_EMBEDDER = None
_RERANKER = None
_SPARSE = None
_models_ready = shared_ready()
_warm_lock = threading.Lock()
_warm_started = False

LOADING_NOTE = "loading local models — call this tool again"
EVIDENCE_CHAR_CAP = 12000  # payload cap; `truncated` flags longer chunks


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
        with _state_lock:
            c = _clients.get(profile.credential_ref)
            if c is None:
                token = get_credential(_workdir(), profile.credential_ref)
                if not token:
                    raise SetupError(
                        "no stored token — run `confluence-knowledge setup` to configure credentials")
                c = _clients[profile.credential_ref] = make_client(profile, token)
    return c


def _read_index(s: dict) -> Index:
    """The index a scope READS from — its own DB normally, the ancestor's
    when the scope is a #240 descendent alias. Write paths always use
    `_index_for(s["key"])` directly."""
    return _index_for(s.get("reads_from") or s["key"])


def _subtree_allow(s: dict, idx: Index) -> set[str] | None:
    """Chunk set a descendent alias may see inside the ancestor snapshot —
    None for a normal (whole-index) scope."""
    if not s.get("subtree_root"):
        return None
    pages = idx.subtree_pages(s["subtree_root"])
    return idx.subtree_chunk_ids(pages) if pages else set()


def _index_for(scope_key: str, *, for_write: bool = False) -> Index:
    """In proxy mode the index opens read-only — writes are physically
    impossible from the proxy process (#189 safeguard)."""
    ro = _daemon_on() and not for_write
    cache_key = (scope_key, ro)
    idx = _indexes.get(cache_key)
    if idx is None:
        with _state_lock:
            idx = _indexes.get(cache_key)
            if idx is None:
                idx = _indexes[cache_key] = Index(
                    workspace_index_root(_workdir()) / f"{scope_key}.db",
                    readonly=ro)
    return idx


def _feedback_for(scope_key: str) -> FeedbackStore:
    st = _feedback.get(scope_key)
    if st is None:
        with _state_lock:
            st = _feedback.get(scope_key)
            if st is None:
                st = _feedback[scope_key] = FeedbackStore(
                    workspace_index_root(_workdir()), scope_key, "confluence-knowledge")
    return st


def _any_worker_alive() -> bool:
    return any(t.is_alive() for t in list(_workers.values()))


def _on_models_unloaded() -> None:
    """Pool dropped the models — clear the mirrors, re-arm the warm gate,
    and release the per-index search caches that pin large matrices."""
    global _EMBEDDER, _RERANKER, _SPARSE, _warm_started
    _EMBEDDER = _RERANKER = _SPARSE = None
    with _warm_lock:
        _warm_started = False
    _models_ready.clear()
    for idx in _indexes.values():
        idx.drop_search_cache()


_pool = shared_pool(_home, "confluence-knowledge", busy_fn=_any_worker_alive,
                  on_unload=_on_models_unloaded,)


def _embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        fake = fake_models()
        _EMBEDDER = fake[0] if fake else _pool.embedder()
    return _EMBEDDER


def _reranker():
    global _RERANKER
    if _RERANKER is None:
        fake = fake_models()
        _RERANKER = fake[1] if fake else _pool.reranker()
    return _RERANKER


def _sparse():
    global _SPARSE
    if _SPARSE is None:
        fake = fake_models()
        _SPARSE = fake[2] if fake else _pool.sparse()
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
    """Load the ONNX sessions off the request path; the first load can
    take longer than an IDE's request timeout."""
    global _warm_started
    with _warm_lock:
        if _warm_started:
            return
        if _daemon_on():
            return      # models live in the daemon — proxy
                        # warmup just doubles RSS (#196)
        _warm_started = True
    threading.Thread(target=_warm_models, name="model-warmup",
                     daemon=True).start()


def _models_loaded(wait: float) -> bool:
    _start_warmup()
    return _models_ready.wait(wait)


def _daemon_url() -> str:
    try:
        from .registration import daemon_url
        return daemon_url()
    except Exception:                       # noqa: BLE001 — status
        return ""                            # must never fail


def _scope(scope_handle: str) -> dict:
    s = _scopes.get(scope_handle)
    if s is not None:
        return s
    # the IDE may restart the server (or the daemon respawned) and drop
    # in-memory scopes; handles carry scope_key+root so they re-bind.
    # Prefer the persisted proof — connect() stamped root_page into the
    # index at first use — over re-running connect()'s live credential
    # check: a VPN/network blip must not cost a cached scope (#229).
    # Security is identical: the mapping exists only where a real
    # connect() created it — a fabricated handle still gets rejected.
    parts = scope_handle.split(":")
    if len(parts) == 3 and parts[0] == "scope":
        key, root = parts[1], parts[2]
        try:
            db = workspace_index_root(_workdir()) / f"{key}.db"
            if not db.exists():     # fabricated key — don't even create
                raise SetupError("unknown scope — call connect first")
            idx = _index_for(key)
            if idx.meta_get("root_page") == root:
                try:
                    profile = SiteProfile.load(_home())
                except SetupError:
                    profile = None      # reads still work; _crawler
                                       # re-checks when a crawl needs it
                s = {"key": key, "profile": profile,
                     "root": root, "client": None}
                alias = idx.meta_get("alias_scope")
                if alias and (workspace_index_root(_workdir())
                              / f"{alias}.db").exists():
                    # #240 descendent alias persists across restarts —
                    # reconstruct the virtual scope offline
                    s["reads_from"] = alias
                    s["subtree_root"] = idx.meta_get("alias_root")
                with _state_lock:
                    _scopes[scope_handle] = s
                return s
        except Exception:                       # noqa: BLE001,S110 — meta
            pass                                # unreadable → old path
        try:
            out = connect(root)
        except Exception:                       # noqa: BLE001 — the
            out = None                          # fallback is best-effort;
        if isinstance(out, dict) and out.get("scope_handle") == scope_handle:
            return _scopes[scope_handle]
    raise SetupError("unknown scope — call connect first")


@tool(mcp)
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
        # identity liveness NEVER blocks on the wire: serve the cached
        # verdict and refresh it off the call path (#173 — a /user/current
        # retry burst made status a >2 s call on hostile networks; it is a
        # light tool). First call computes synchronously so a brand-new
        # server still answers truthfully.
        ref = profile.credential_ref
        with _state_lock:
            cached = _CREDS_CACHE.get(ref)
        fresh = cached and time.time() - cached[0] < _CREDS_TTL_S
        if fresh:
            ok, detail = cached[1], cached[2]
        elif cached is not None:
            ok, detail = cached[1], cached[2]
            _creds_refresh_async(profile)
        else:
            ok, detail = _creds_check(profile)
            with _state_lock:
                _CREDS_CACHE[ref] = (time.time(), ok, detail)
        checks.append({"flag": "creds_valid", "ok": ok, "detail": detail,
                       "remediation": None if ok else
                       "confluence-knowledge setup (rotate the token)"})
    if _daemon_on():
        # the http endpoint is the reconnectable transport (#206): the IDE
        # points at the daemon's URL — a dead daemon or stale port means
        # tools go unreachable until doctor self-heals it
        try:
            from .registration import daemon_http_status
            checks.append(daemon_http_status())
        except Exception:                       # noqa: BLE001,S110 —
            pass                                # status must never fail
    last = None
    try:
        # the freshest scope index carries the most recent job — enough
        # for the agent to answer "did my crawl finish?" without polling
        cands = sorted((x for x in idx_root.glob("*.db")
                        if x.name != "rpc.db"),
                       key=lambda p: -p.stat().st_mtime)
        if cands:
            last = _last_crawl(_index_for(cands[0].stem))
    except Exception:  # noqa: BLE001 — status must never fail on this
        last = None
    return {
        "configured": "error" not in cfg,
        "profile": cfg,
        "checks": checks,
        "models_loaded": ((_daemon_call("daemon_models_ready", {}, 10)
                          or {}).get("ready", False) if _daemon_on()
                          else _models_ready.is_set()
                          and (_EMBEDDER is not None or _pool.is_loaded())),
        "index_root": str(idx_root),
        # every path that participates in cross-IDE sharing (#257): two
        # installs disagreeing on any of these silently forks the index —
        # diff this block between IDEs when "the same root" answers
        # differently
        "paths": {
            "index_root": str(idx_root),
            "home": str(_home()),
            "workdir": str(_workdir()),
            "daemon_home": str(daemon_home()),
            "daemon_url": _daemon_url(),
        },
        "daemon": ("running" if _daemon_on() and _rpc().daemon_alive()
                   else "off" if not _daemon_on() else "starting"),
        "live_scopes": sorted(_scopes),
        "last_crawl": last,
        # the zero-terminal path (#191): one absolute copy-pasteable
        # string for the privacy-first route; chat path is clipboard-based
        "setup_cmd": f"{sys.executable} -m confluence_knowledge.cli setup",
        "setup_cmd_clipboard": (
            f"{sys.executable} -m confluence_knowledge.cli setup "
            "--site <URL> --email <EMAIL> --env cloud --from-clipboard"),
        # the off-switches — discoverable from the tool itself, because
        # a `pkill` is NOT a control path (the watchdog respawns the
        # daemon and the crawl resumes). `stop` shuts down the daemon
        # + watchdog cleanly; `cancel_crawl` halts a running crawl and
        # keeps the tools alive.
        "stop_cmd": f"{sys.executable} -m confluence_knowledge.cli stop",
        "cancel_crawl_tool": "cancel_crawl(scope_handle, job_id)",
    }


@tool(mcp)
def register_token(site: str, env: str = "cloud", email: str = "") -> dict:
    """Register the Confluence credential without exposing the token.

    Reads the OS clipboard inside this process — the token is NEVER a
    tool argument, so it cannot transit the chat model or land in chat
    history. Validates the token against the Confluence API BEFORE
    persisting, stores it in the OS keyring, returns a masked confirm.

    Usage: ask the user to copy their PAT/API token, then call this.
      cloud      — *.atlassian.net — email required
      datacenter — corporate Confluence — email ignored
    """
    from .cli import register_clipboard_credential
    try:
        return register_clipboard_credential(env, site, email or None)
    except SetupError as e:
        return {"configured": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001 — auth/network/TLS → honest err
        return {"configured": False,
                "error": f"connection check failed — nothing stored: {e}"}


@tool(mcp)
def connect(root: str) -> dict:
    """Validate credentials + root readability; bind a scope handle."""
    out = _daemon_call("connect", {"root": root}, 120)
    if out is not None:
        h = out.get("scope_handle")
        if h and h.startswith("scope:"):
            parts = h.split(":")
            key = parts[1]
            with _state_lock:
                _scopes[h] = {"key": key, "root": parts[2],
                              "client": None, "profile": None}
        return out
    if _daemon_on():
        # writes may never bypass the daemon (#189 safeguard 2) — a
        # transient spawn/claim gap is a retryable error, never a local
        # crawl on the read-only index
        return {"_error": "daemon unavailable — retry in a few seconds",
                "daemon": "starting"}


    profile = SiteProfile.load(_home())
    root_id = extract_page_id(root)
    _clients.pop(profile.credential_ref, None)  # re-read the token: setup may have rotated it
    with _state_lock:
        _CREDS_CACHE.pop(profile.credential_ref, None)  # force re-validate
    client = _client_for(profile)
    who = client.get("wiki/rest/api/user/current" if profile.env == "cloud"
                     else "rest/api/user/current").json()
    with _state_lock:
        _CREDS_CACHE[profile.credential_ref] = \
            (time.time(), True, "token valid")   # connect just proved it
    principal = who.get("accountId") or who.get("userKey") or who.get("username") or "?"
    scope_key = profile.scope_key(principal, root_id)
    idx = _index_for(scope_key, for_write=True)
    idx.meta_set("root_page", root_id)     # daemon orphan resume (#189)
    idx.meta_set("principal", principal)   # offline orphan reclaim (#231)
    st = idx.stats()
    covering = None
    if st["pages"] == 0:
        # #240: a descendent of a fully-indexed ancestor answers through
        # the ancestor's snapshot — no re-crawl, no re-embed
        covering = find_covering(
            workspace_index_root(_workdir()), scope_key,
            covers=lambda a: len(a.subtree_pages(root_id)),
            principal=principal)
        if covering:
            idx.meta_set("alias_scope", covering["key"])
            idx.meta_set("alias_root", root_id)
    handle = f"scope:{scope_key}:{root_id}"
    s = {"key": scope_key, "profile": profile, "root": root_id}
    if covering:
        s["reads_from"] = covering["key"]
        s["subtree_root"] = root_id
    with _state_lock:
        _scopes[handle] = s
    _start_warmup()
    stale = idx.model_id() is not None and idx.model_id() != EMBED_MODEL_ID
    auto = _maybe_auto_reindex(_scopes[handle], idx) if stale else None
    out = {
        "scope_handle": handle,
        "root_page_id": root_id,
        "principal": principal,
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
    if covering:
        anc = _index_for(covering["key"])
        out.update({
            "cached": True,
            "cached_pages": covering["pages"],
            "cached_as_of": covering["as_of"] or None,
            "covered_by": anc.meta_get("root_page"),
            "note": "root is inside an indexed ancestor scope — "
                    "answering from the ancestor's index. "
                    "start_crawl(scope_handle) builds a dedicated index "
                    "if you need fresher data."})
    return out


def _maybe_auto_reindex(s: dict, idx: Index) -> str | None:
    """#178: a stale-model index self-heals — notify once + start the
    rebuild crawl automatically unless a job is already in flight."""
    latest = idx.latest_job()
    if latest and latest["state"] in ("running", "queued"):
        return None                               # already rebuilding
    if f"model:{s['key']}" not in _notified:
        _notified.add(f"model:{s['key']}")
        notify("confluence-knowledge",
               "index was built with an older embedding model — "
               "re-indexing so search stays compatible")
    if os.environ.get("CONF_KNOWLEDGE_AUTO_REINDEX", "1") == "0":
        return None
    job_id = _crawler(s).start(s["root"])
    return "started" if _ensure_worker(s, job_id) else "queued"


def _crawler(s: dict, embedder=None, sparse=None, control=None) -> Crawler:
    profile = s["profile"] or SiteProfile.load(_home())
    s["profile"] = profile              # rebind may carry None (#229)

    def _fresh_client():
        # re-read the credential — a setup-rotated token must reach the
        # running crawl without a restart (#177)
        with _state_lock:
            _clients.pop(profile.credential_ref, None)
            _CREDS_CACHE.pop(profile.credential_ref, None)
        return _client_for(profile)

    return Crawler(_client_for(profile), _index_for(s["key"]), profile.env,
                   embedder=embedder, sparse=sparse,
                   site_url=profile.base_url, control=control,
                   client_factory=_fresh_client)


# background crawl workers — a crawl runs to completion server-side so a big
# subtree doesn't need hundreds of tool calls (each one an IDE LLM turn and a
# timeout surface, #81). crawl_step is then only a progress read.
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
    body = (f"{n} pages indexed" if job["state"] == "done"
            else f"{n} pages seen")
    if errs:
        body += f" — {len(errs)} errors, last: {errs[-1][:60]}"
    notify(f"{label} — confluence-knowledge", body)


_RPC: RpcDb | None = None


def index_root() -> Path:
    """Daemon-facing alias — the control db lives beside the scope dbs."""
    return workspace_index_root(_workdir())


def daemon_models_ready() -> dict:
    """RPC-only callable — _daemon_call routes here; the daemon answers
    its own warmup state. Not an MCP tool (no @tool)."""
    return {"ready": _models_ready.is_set()}


def _daemon_on() -> bool:
    """Proxy mode (#189): opt-in via CONF_KNOWLEDGE_DAEMON=1. The daemon itself sets
    KNOWLEDGE_DAEMON_ROLE so its imported tools never re-proxy."""
    return daemon_enabled("CONF_KNOWLEDGE_DAEMON")


def _rpc() -> RpcDb:
    global _RPC
    if _RPC is None:
        _RPC = RpcDb(daemon_home() / "rpc.db")
    return _RPC


def _daemon_call(name: str, args: dict, timeout: float) -> dict | None:
    """Submit to the daemon and wait on the response row. None = the
    daemon is unreachable — caller falls back to running locally."""
    if not _daemon_on():
        return None
    try:
        rpc = _rpc()
        ensure_daemon(rpc)
        rid = rpc.submit(name, args, timeout,
                         service="confluence-knowledge")
        return rpc.wait(rid, timeout,
                        on_tick=lambda: heartbeat(
                            0, message=f"daemon {name}"))
    except DaemonUnavailable as e:
        return {"_error": f"daemon unavailable: {e}"}
    except RuntimeError as e:
        return {"_error": str(e)}


def _resume_job(scope_key: str, job_id: str) -> None:
    """Daemon-side hook: rebuild a crawler for a job orphaned by a
    daemon restart — the scope's root persists in index meta (#189).
    (scope_key needs principal+root — both were previously called
    wrong, so this path was dead until the journey e2e caught it.)"""
    idx = _index_for(scope_key)
    root = idx.meta_get("root_page")
    if not root:
        return
    profile = SiteProfile.load(_home())
    principal = idx.meta_get("principal")
    if principal is None:
        # pre-#231 index — the only validation is the live check
        try:
            client = _client_for(profile)
            who = client.get("wiki/rest/api/user/current"
                             if profile.env == "cloud"
                             else "rest/api/user/current").json()
            principal = (who.get("accountId") or who.get("userKey")
                         or who.get("username") or "?")
        except Exception:               # noqa: BLE001 — offline:
            return                      # keep the job orphaned rather
    if profile.scope_key(principal, root) != scope_key:  # than guess
        return          # profile moved to another site — not ours
    s = {"key": scope_key, "root": root,
         "client": None, "profile": profile}   # _crawler rebuilds lazily
    t = threading.Thread(target=_worker_loop, args=(s, job_id),
                         daemon=True, name=f"crawl-{job_id}")
    _workers[job_id] = t
    t.start()


def _owner_label(idx, job_id: str) -> str:
    """Lease-holder label — 'the knowledge daemon' or 'another server
    process' (#186 wording; #189 marks daemon owners explicitly)."""
    try:
        from confluence_knowledge.rpc import lease_label
        return lease_label(idx.lease_owner(job_id))
    except Exception:                       # noqa: BLE001 — label is
        return "another server process"     # cosmetic, never block

def _worker_loop(s: dict, job_id: str) -> None:
    idx = _index_for(s["key"], for_write=True)
    try:
        # models may still be warming (or were idle-unloaded); the worker
        # waits, not the tool call
        _start_warmup()
        _models_ready.wait()
        prog = _progress.setdefault(job_id, Progress())
        c = _crawler(s, embedder=_embedder(), sparse=_sparse(),
                     control=crawl_control(
                         idx, job_id, prog,
                         _cancel_flags.setdefault(job_id, threading.Event()),
                         _owner, notify=notify))
        while True:
            r = c.step(job_id, batch=None, budget_s=None)
            if r["state"] != "running":
                break
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


def _resume_worker(s: dict, job_id: str) -> None:
    """Resume a running job's worker. Direct mode: spawn the local
    thread as before. Proxy mode: respawn the daemon — its boot pass
    reclaims orphans; a proxy must never run a write worker itself
    (safeguard 2)."""
    if _daemon_on():
        try:
            ensure_daemon(_rpc())
        except DaemonUnavailable:
            pass                       # job stays 'running' — retry
    else:
        _ensure_worker(s, job_id)


def _ensure_worker(s: dict, job_id: str) -> bool:
    """Start the crawl worker if none is alive — also the resume path after
    the server restarts mid-crawl (the queue is durable, the thread isn't).
    The cross-process lease makes it exactly one worker per job across all
    IDE windows; False when another live process owns the crawl."""
    key = f"{s['key']}:{job_id}"
    with _workers_lock:
        t = _workers.get(key)
        if t is not None and t.is_alive():
            return True
        idx = _index_for(s["key"], for_write=True)
        if not idx.acquire_lease(job_id, _owner.value, _owner.pid):
            prog = _progress.setdefault(job_id, Progress())
            prog.worker = _owner_label(_index_for(s["key"]), job_id)
            return False
        prog = _progress.setdefault(job_id, Progress())
        prog.worker = "self"
        _workers[key] = threading.Thread(
            target=_worker_loop, args=(s, job_id),
            name=f"crawl-{job_id}", daemon=True)
        _workers[key].start()
        return True


def _progress_for(idx: Index, job_id: str) -> dict:
    """Merge durable job progress with the live registry (or the persisted
    blob another process checkpoints) — phase/rate/worker come along."""
    p = idx.job_progress(job_id)
    live = _progress.get(job_id)
    # a non-owning process's Progress() is all zeros — merging it would
    # clobber the owner's durable checkpoint (#186): fall back to the
    # persisted blob instead
    if live is not None and live.worker == "self":
        p.update(live.snapshot())
    else:
        blob = idx.meta_get(f"progress:{job_id}")
        other = Progress.from_blob(blob) if blob else None
        if other is not None:
            # the owner's blob can lag durable meta — take max so a
            # stale/early snapshot never lowers the visible truth
            seen_raw = idx.meta_get(f"seen:{job_id}")
            seen_n = len(json.loads(seen_raw)) if seen_raw else 0
            p.update(other.snapshot())
            p["pages_seen"] = max(p.get("pages_seen", 0), seen_n)
            # name the real lease holder — 'the knowledge daemon' when
            # the brain owns it (#186/#189), not a generic label
            p["worker"] = _owner_label(idx, job_id)
    if live is not None and live.worker != "self":
        p["worker"] = live.worker
    # errors_n derives from the durable error list — a snapshot written
    # just before the last appends can lag; len(errors) is always true
    p["errors_n"] = max(p.get("errors_n", 0), len(p.get("errors", [])))
    # three-tier reporting (#187) — pulse + stages live, audit terminal
    if p.get("state") == "running":
        p["pulse"] = pulse(p, current_rss_mb())
        p["stages"] = stages(p)
    elif p.get("state") in ("done", "error", "cancelled", "abandoned"):
        p["summary"] = audit(idx, job_id)
    return p


def _last_crawl(idx: Index) -> dict | None:
    """Latest job summary for status/connect — lets the agent answer
    'did the crawl finish?' without a second tool call."""
    job = idx.latest_job()
    if job is None:
        return None
    out = {"job_id": job["job_id"], "state": job["state"],
           "errors_n": len(job["errors"])}
    pub = idx.meta_get(f"publish:{job['job_id']}")
    if pub:
        out["publish"] = json.loads(pub)   # staged/deleted/kept (#176)
    live = _progress.get(job["job_id"])
    blob = idx.meta_get(f"progress:{job['job_id']}")
    other = Progress.from_blob(blob) if blob else None
    snap = (live.snapshot()
            if live is not None and live.worker == "self"
            else (other.snapshot() if other is not None else None))
    if snap:
        for k in ("pages_done", "phase", "rate_ppm", "eta_s", "worker"):
            if snap.get(k) is not None:
                out[k] = snap[k]
    # #187: pulse while running, audit summary on terminal jobs
    if out["state"] == "running":
        merged = dict(snap or {})
        merged.update({"state": "running"})
        out["pulse"] = pulse(merged, current_rss_mb())
        out["stages"] = stages(merged)
    elif out["state"] in ("done", "error", "cancelled", "abandoned"):
        out["summary"] = audit(idx, job["job_id"])
    return out


@tool(mcp)
def start_crawl(scope_handle: str, refresh: bool = False) -> dict:
    """Begin crawling the root page and all descendants in a background
    thread. The crawl runs to completion without further input — call
    crawl_step only to read progress."""
    out = _daemon_call("start_crawl", {"scope_handle": scope_handle,
                                       "refresh": refresh}, 60)
    if out is not None:
        return out
    if _daemon_on():
        # writes may never bypass the daemon (#189 safeguard 2) — a
        # transient spawn/claim gap is a retryable error, never a local
        # crawl on the read-only index
        return {"_error": "daemon unavailable — retry in a few seconds",
                "daemon": "starting"}

    s = _scope(scope_handle)
    if s.get("reads_from"):
        # #240: an aliased descendent asked for real data — materialize:
        # clone the subtree from the ancestor into this scope's own DB,
        # then crawl normally. content_hash carries over so unchanged
        # pages keep their embeddings via live_page_hash/copy_live_to_stage.
        own = _index_for(s["key"], for_write=True)
        anc = _index_for(s["reads_from"])
        own.import_subtree(anc, anc.subtree_pages(s["subtree_root"]))
        own.meta_del("alias_scope")
        own.meta_del("alias_root")
        s.pop("reads_from", None)
        s.pop("subtree_root", None)
    job_id = _crawler(s).start(s["root"])
    _cancel_flags.pop(job_id, None)
    _progress[job_id] = Progress()
    if not _ensure_worker(s, job_id):
        return {"job_id": job_id, "state": "running",
                "worker": _owner_label(_index_for(s["key"]), job_id),
                "note": "crawl runs under another process — crawl_step to poll"}
    return {"job_id": job_id, "state": "running",
            "note": "crawl runs in the background — call crawl_step to poll"}


@tool(mcp)
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
        if not _daemon_on() and not _models_ready.is_set():
            _start_warmup()
            note = LOADING_NOTE
        else:
            note = None
        _resume_worker(s, job_id)
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
                   wait_s: int = 25, ctx: Context | None = None) -> dict:
    """Long-poll for a crawl change — returns as soon as the state or
    progress changes, or after `wait_s` (hard-capped at 45 s — a longer
    in-flight request outlives any IDE tool timeout, #206). Each poll emits
    an MCP progress notification — a keep-alive that resets the client's
    request timeout without knowing its value; the 25 s default also
    returns well under typical IDE tool timeouts. `changed` tells
    whether anything moved."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"])
    job = idx.job(job_id)
    if job is None:
        raise AccessError(f"unknown job {job_id}")
    if job["state"] == "running":
        if not _daemon_on() and not _models_ready.is_set():
            _start_warmup()
        _resume_worker(s, job_id)   # resume after a server restart too
    # 45s ceiling: a longer in-flight request outlives any client request
    # timeout (the field's -32001) — beyond it we return changed:false
    # and the agent re-calls (#206). The wait_s param still controls the
    # intended wait up to the cap.
    wait_s = max(1, min(int(wait_s), 45))
    deadline = time.monotonic() + wait_s
    wall_deadline = time.time() + wait_s + 10
    last_wall = time.time()
    prev = _progress_for(idx, job_id)
    while prev["state"] == "running" and time.monotonic() < deadline             and time.time() < wall_deadline:
        time.sleep(2)
        now = time.time()
        cur = _progress_for(idx, job_id)
        if now - last_wall > 15:
            # wall-clock jump = the machine suspended mid-wait — hand the
            # agent something honest instead of a client-side timeout
            return {**cur, "changed": False,
                    "note": "the computer slept — the crawl kept running "
                            "in the background; poll again"}
        last_wall = now
        moved = (cur["state"] != prev["state"]
                 or cur.get("phase") != prev.get("phase")
                 or cur.get("pages_done") != prev.get("pages_done"))
        if moved:
            cur["changed"] = True
            return cur
        # heartbeat — resets the client's request timeout regardless of
        # its IDE-specific limit; the progressToken-less case no-ops
        heartbeat(cur.get("pages_done") or 0,
                  total=cur.get("total_pages"),
                  message=f"{cur.get('phase') or cur.get('state')} "
                          f"({cur.get('pages_done') or 0} pages)")
        prev = cur
    prev["changed"] = False
    return prev


@tool(mcp)
def cancel_crawl(scope_handle: str, job_id: str) -> dict:
    """Stop a running crawl within seconds and mark the job cancelled — it
    never auto-resumes. The previously published snapshot is kept intact;
    start_crawl re-runs (unchanged pages are reused)."""
    out = _daemon_call("cancel_crawl", {"scope_handle": scope_handle,
                                        "job_id": job_id}, 30)
    if out is not None:
        return out
    if _daemon_on():
        # writes may never bypass the daemon (#189 safeguard 2) — a
        # transient spawn/claim gap is a retryable error, never a local
        # crawl on the read-only index
        return {"_error": "daemon unavailable — retry in a few seconds",
                "daemon": "starting"}


    s = _scope(scope_handle)
    idx = _index_for(s["key"], for_write=True)
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
                    "(unchanged pages are reused)"}


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
    out = _daemon_call("search", {"scope_handle": scope_handle,
                                  "question": question}, 90)
    if out is not None:
        return out

    s = _scope(scope_handle)
    idx = _read_index(s)
    st = idx.stats()
    if st["pages"] == 0:
        job = _index_for(s["key"]).latest_job()
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
    emb = _embedder()
    ev = retrieve(idx, question, emb, _reranker(), sparse=_sparse(),
                  allow_chunks=_subtree_allow(s, idx))
    # saved corrections matching this question ride along — the agent
    # must disclose them, so they're part of the response, not a separate call
    corrections = _feedback_for(s["key"]).match(
            emb.embed([question], query=True)[0])
    # unique immutable handle — never derived from cache size; bound to the
    # snapshot epoch so a refresh can't silently re-point a citation
    set_id = f"ev:{s['key'][:8]}:{uuid.uuid4().hex[:16]}"
    # bound the session cache — sets are never reused after the turn ends
    with _state_lock:
        while len(_evidence_sets) >= 32:
            _evidence_sets.pop(next(iter(_evidence_sets)))
        _evidence_sets[set_id] = {"scope": s["key"], "ev": ev,
                                  "epoch": idx.snapshot_epoch()}
    idx.persist_evset(set_id, [e["chunk_id"] for e in ev],
                      _evidence_sets[set_id]["epoch"])
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
    idx = _read_index(s)
    pages = idx.active_pages()
    if s.get("subtree_root"):
        keep = idx.subtree_pages(s["subtree_root"])
        pages = {pid: p for pid, p in pages.items() if pid in keep}
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
            if c is None or (s.get("subtree_root")
                             and c["page_id"] not in pages):
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
    out = _daemon_call("submit_feedback",
                       {"scope_handle": scope_handle, "question": question,
                        "correction": correction, "note": note}, 30)
    if out is not None:
        return out
    if _daemon_on():
        # writes may never bypass the daemon (#189 safeguard 2) — a
        # transient spawn/claim gap is a retryable error, never a local
        # crawl on the read-only index
        return {"_error": "daemon unavailable — retry in a few seconds",
                "daemon": "starting"}


    s = _scope(scope_handle)
    if not _models_loaded(0):
        return {"stored": False, "note": LOADING_NOTE}
    qvec = _embedder().embed([question], query=True)[0]
    entry = _feedback_for(s["key"]).add(question, qvec, correction, note)
    return {"stored": entry["id"],
            "note": "will be disclosed on similar questions"}


@tool(mcp)
def feedback_report(scope_handle: str) -> dict:
    """Aggregated corrections — the agent infers which doc keeps
    getting corrected and suggests it to the user."""
    out = _daemon_call("feedback_report", {"scope_handle": scope_handle}, 30)
    if out is not None:
        return out

    s = _scope(scope_handle)
    return _feedback_for(s["key"]).report()


@tool(mcp)
def verify_citations(scope_handle: str, evidence_set: str,
                     mappings: list[dict]) -> dict:
    # evidence sets live in whichever process ran `search` — under the
    # daemon that's the daemon, so this must route there (the proxy's
    # _evidence_sets is empty → "unknown evidence set" on every call)
    out = _daemon_call("verify_citations",
                       {"scope_handle": scope_handle,
                        "evidence_set": evidence_set,
                        "mappings": mappings}, 60)
    if out is not None:
        return out
    s = _scope(scope_handle)  # binds or SetupError — never KeyError below
    with _state_lock:
        rec = _evidence_sets.get(evidence_set)
    idx = _read_index(s)          # aliased scopes' evsets live in the
                                  # ancestor's meta (#240)
    if rec is None:
        # daemon restarted — rebuild from the persisted shadow (#231)
        r = idx.evset_rebind(evidence_set)
        if r:
            rec = {"scope": s["key"], **r}
    # full 24-char scope key — the key[:8] prefix in the id is cosmetic
    if rec is None or rec["scope"] != s["key"]:
        raise SecurityException(
            "unknown or cross-scope evidence set — run search again and cite its evidence_set")
    if rec.get("epoch") != idx.snapshot_epoch():
        raise SecurityException(
            "evidence set predates the latest published snapshot — "
            "run search again and cite the fresh evidence_set")
    return check_citations(rec["ev"], mappings)


def main() -> None:
    # httpx logs every request URL at INFO; keep the IDE's MCP log readable
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # stay a polite background citizen: lowered priority, no eager model
    # load (an idle server costs ~0.2 GB, not ~2 GB), models released
    # after a stretch of inactivity
    lower_priority()
    _pool.start_reaper()
    mcp.run()


if __name__ == "__main__":
    main()
