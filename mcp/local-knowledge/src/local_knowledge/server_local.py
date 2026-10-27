"""stdio MCP server for local-knowledge — the only surface the agent reaches.

Same tool surface as confluence-knowledge minus the credential machinery:
there is nothing to authenticate. Scope handles are opaque paths bound to the
canonical realpath partition.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP

import confluence_knowledge.index as _index_mod
from confluence_knowledge.client import AccessError, SecurityException
from confluence_knowledge.feedback import FeedbackStore
from confluence_knowledge.index import Index, find_covering
from confluence_knowledge.jobs import (
    CrawlCancelled,
    LeaseOwner,
    Progress,
    crawl_control,
)
from confluence_knowledge.mcp_async import heartbeat, tool
from confluence_knowledge.models import shared_pool, shared_ready
from confluence_knowledge.notify import notify
from confluence_knowledge.reporting import (
    audit,
    current_rss_mb,
    pulse,
    stages,
)
from confluence_knowledge.resources import lower_priority
from confluence_knowledge.retrieval import (
    MODEL_MARKER as EMBED_MODEL_ID,
)
from confluence_knowledge.retrieval import (
    backfill_tokvecs,
    check_citations,
    fake_models,
    retrieve,
)
from confluence_knowledge.rpc import (
    DaemonUnavailable,
    RpcDb,
    daemon_enabled,
    ensure_daemon,
)
from confluence_knowledge.settings import SetupError, daemon_home

from .crawl_local import LocalCrawler, resolve_root
from .settings_local import APP_NAME, LocalProfile, home, index_root, workdir

mcp = FastMCP("local-knowledge")

# tools run on worker threads now (mcp_async) — one lock guards the caches
_state_lock = threading.RLock()
_scopes: dict[str, dict] = {}
_evidence_sets: dict[str, dict] = {}  # set_id → {scope, ev}
_indexes: dict[str, Index] = {}
_feedback: dict[str, FeedbackStore] = {}
_owner = LeaseOwner()   # process identity for cross-process crawl leases
_progress: dict[str, Progress] = {}  # job_id → live progress (lock-free reads)
_cancel_flags: dict[str, threading.Event] = {}  # job_id → cancel requested
# module globals kept for test injection; the pool owns real model objects
_EMBEDDER = None
_RERANKER = None
_SPARSE = None
_COLBERT = None
_models_ready = shared_ready()
_warm_lock = threading.Lock()
_warm_started = False

LOADING_NOTE = "loading local models — call this tool again"
EVIDENCE_CHAR_CAP = 12000  # payload cap; `truncated` flags longer chunks


def _read_index(s: dict) -> Index:
    """Index the scope READS — its own DB normally, the ancestor's when
    the scope is a #240 descendent alias (reads_from)."""
    return _index_for(s.get("reads_from") or s["key"])


def _subtree_allow(s: dict, idx: Index) -> set[str] | None:
    if not s.get("subtree_root"):
        return None
    pages = idx.subtree_pages_prefix(s["subtree_root"])
    return idx.subtree_chunk_ids(pages) if pages else set()


def _index_for(scope_key: str, *, for_write: bool = False) -> Index:
    """In proxy mode the index opens read-only — writes are physically
    impossible from the proxy process (OperationalError), which is the
    'strictly read-only' safeguard from #189."""
    ro = _daemon_on() and not for_write
    cache_key = (scope_key, ro)
    idx = _indexes.get(cache_key)
    if idx is None:
        with _state_lock:
            idx = _indexes.get(cache_key)
            if idx is None:
                idx = _indexes[cache_key] = Index(
                    index_root() / f"{scope_key}.db", readonly=ro)
    return idx

def _feedback_for(scope_key: str) -> FeedbackStore:
    st = _feedback.get(scope_key)
    if st is None:
        with _state_lock:
            st = _feedback.get(scope_key)
            if st is None:
                st = _feedback[scope_key] = FeedbackStore(
                    index_root(), scope_key, "local-knowledge")
    return st


def _any_worker_alive() -> bool:
    return any(t.is_alive() for t in list(_workers.values()))


_STALE_CODE = False
_WATCHER_STARTED = None   # generation sentinel — see _start_stale_watcher


def _installed_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("local-knowledge")
    except Exception:            # noqa: BLE001 — not installed (src run)
        return None               # → nothing to compare against


def _start_stale_watcher() -> None:
    """#272: a stdio server keeps the code it spawned with — `pip -U`
    leaves it stale until window reload. Watch the installed version;
    on skew confirmed by two polls (debounce for a mid-install flap)
    log to stderr, expose `stale_code` on status, and exit so the
    client's next request respawns fresh code. Deferred while a crawl
    worker lives — killing it just hands the resumable orphan to
    another window. KNOWLEDGE_PROXY_SELFKILL=0 disables."""
    global _WATCHER_STARTED
    if _WATCHER_STARTED is not None:
        return
    try:
        poll = float(os.environ.get("KNOWLEDGE_PROXY_POLL_S", "60"))
    except ValueError:
        poll = 60.0
    loaded = _installed_version()
    if (poll <= 0 or loaded is None
            or os.environ.get("KNOWLEDGE_PROXY_SELFKILL") == "0"):
        return
    _WATCHER_STARTED = gen = object()

    def watch() -> None:
        global _STALE_CODE
        skew = 0
        # the generation sentinel is the kill switch: clearing
        # _WATCHER_STARTED ends THIS watcher only — a later armer gets a
        # new sentinel and can't resurrect dead generations (monkeypatch
        # teardown relies on that so watchers never outlive a test)
        while _WATCHER_STARTED is gen:
            time.sleep(poll)
            cur = _installed_version()
            if cur == loaded:
                skew = 0
                _STALE_CODE = False
                continue
            if cur is None:       # metadata briefly gone mid-install
                continue
            skew += 1
            _STALE_CODE = True
            print(f"knowledge: installed {cur} != loaded {loaded} — "
                  "will exit for a fresh respawn", file=sys.stderr)
            if (skew >= 2 and _WATCHER_STARTED is gen
                    and not _any_worker_alive()):
                os._exit(0)

    threading.Thread(target=watch, daemon=True,
                     name="stale-watcher").start()


def _on_models_unloaded() -> None:
    global _EMBEDDER, _RERANKER, _SPARSE, _COLBERT, _warm_started
    _EMBEDDER = _RERANKER = _SPARSE = _COLBERT = None
    with _warm_lock:
        _warm_started = False
    _models_ready.clear()
    for idx in _indexes.values():
        idx.drop_search_cache()


_pool = shared_pool(home, APP_NAME, busy_fn=_any_worker_alive,
                  on_unload=_on_models_unloaded,)


def _colbert():
    global _COLBERT
    if _COLBERT is None:
        fake = fake_models()
        _COLBERT = fake[3] if fake and len(fake) > 3 else _pool.colbert()
    return _COLBERT


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
        pass
    finally:
        _models_ready.set()


def _start_warmup() -> None:
    global _warm_started
    with _warm_lock:
        if _warm_started:
            return
        if _daemon_on():
            return      # models live in the daemon — proxy
                        # warmup just doubles RSS (#196)
        # ColBERT tokvec backfill rides every publish's idle-time warm
        # thread (#329) — registered at server boot so test processes
        # never spin up model sessions or background index writes.
        _index_mod.POST_PUBLISH = \
            lambda i: backfill_tokvecs(i, _pool.colbert)
        _warm_started = True
    threading.Thread(target=_warm_models, name="model-warmup",
                     daemon=True).start()


def _models_loaded(wait: float) -> bool:
    _start_warmup()
    return _models_ready.wait(wait)


def _daemon_url() -> str:
    try:
        from confluence_knowledge.registration import daemon_url
        return daemon_url()
    except Exception:                       # noqa: BLE001 — status
        return ""                            # must never fail


def _scope(scope_handle: str) -> dict:
    s = _scopes.get(scope_handle)
    if s is not None:
        return s
    # daemon/server restart dropped _scopes — re-bind from the persisted
    # proof (connect() stamps root_path) instead of re-running connect():
    # identical security (the meta exists only where a real connect
    # created it) and zero side-effects (#229).
    parts = scope_handle.split(":", 2)
    if len(parts) == 3 and parts[0] == "scope":
        key, root = parts[1], parts[2]
        try:
            if not (index_root() / f"{key}.db").exists():
                raise SetupError("unknown scope — call connect first")
            idx = _index_for(key)
            if idx.meta_get("root_path") == root:
                s = {"key": key, "root": Path(root)}
                alias = idx.meta_get("alias_scope")
                if alias and (index_root() / f"{alias}.db").exists():
                    # #240 alias persists across restarts — recompute the
                    # rel prefix under the ancestor root, offline
                    aroot = (_index_for(alias).meta_get("root_path") or "")
                    if aroot:
                        try:
                            s["reads_from"] = alias
                            s["subtree_root"] = os.path.relpath(
                                root, Path(aroot).parent).replace("\\", "/")
                        except ValueError:
                            pass        # drive changed → stay uncached
                with _state_lock:
                    _scopes[scope_handle] = s
                return s
        except Exception:                       # noqa: BLE001,S110
            pass
        try:
            out = connect(root)
        except Exception:                       # noqa: BLE001
            out = None
        if isinstance(out, dict) and out.get("scope_handle") == scope_handle:
            return _scopes[scope_handle]
    raise SetupError("unknown scope — call connect first")



_RPC: RpcDb | None = None


def daemon_models_ready() -> dict:
    """RPC-only callable — _daemon_call routes here; the daemon answers
    its own warmup state. Not an MCP tool (no @tool)."""
    return {"ready": _models_ready.is_set()}

def _daemon_on() -> bool:
    """Proxy mode (#189): opt-in via LOCAL_KNOWLEDGE_DAEMON=1 — off by
    default in v1 so the architecture proves itself before becoming the
    default path. The daemon sets KNOWLEDGE_DAEMON_ROLE so its imported
    tools never re-proxy."""
    return daemon_enabled("LOCAL_KNOWLEDGE_DAEMON")


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
                         service="local-knowledge")
        return rpc.wait(rid, timeout,      # keep the MCP request alive
                        on_tick=lambda: heartbeat(
                            0, message=f"daemon {name}"))
    except DaemonUnavailable:
        return None
    except RuntimeError as e:
        return {"_error": str(e)}



def _owner_label(idx, job_id: str) -> str:
    """Lease-holder label — 'the knowledge daemon' or 'another server
    process' (#186 wording; #189 marks daemon owners explicitly)."""
    try:
        from confluence_knowledge.rpc import lease_label
        return lease_label(idx.lease_owner(job_id))
    except Exception:                       # noqa: BLE001 — label is
        return "another server process"     # cosmetic, never block

def _resume_job(scope_key: str, job_id: str) -> None:
    """Daemon-side hook: rebuild a crawler for a job orphaned by a daemon
    restart — the scope's root persists in index meta (#189)."""
    s = {"key": scope_key}
    idx = _index_for(scope_key, for_write=True)
    root_s = idx.meta_get("root_path")
    if not root_s:
        return
    s["root"] = Path(root_s)
    # go through _ensure_worker — a bare spawn keyed _workers[job_id]
    # while _ensure_worker keys scope:job meant a later crawl_step could
    # not see this thread, acquired the (unheld) lease and drove a second
    # worker on the same job → StaleJobError → job error (#257 CI)
    _ensure_worker(s, job_id)


_ORPHANS_SWEPT = False


def _resume_orphaned_crawls() -> None:
    """Local-mode orphan reclaim (#285): the daemon used to sweep
    'running' jobs whose worker died at ITS boot; locally the first
    status/connect call does. _resume_job rebuilds the scope from index
    meta and _ensure_worker wins-or-yields the lease — a job owned by a
    LIVE process is left alone by acquire_lease itself."""
    global _ORPHANS_SWEPT
    if _ORPHANS_SWEPT or _daemon_on():
        return
    _ORPHANS_SWEPT = True
    try:
        dbs = [d for d in index_root().glob("*.db")
               if d.name != "rpc.db"]
    except OSError:
        return
    for db in dbs:
        try:
            idx = _index_for(db.stem)
            job = idx.latest_job()
            if job is not None and job["state"] == "running":
                _resume_job(db.stem, job["job_id"])
        except Exception:       # noqa: BLE001,S112 — best-effort
            continue            # reclaim; a bad index must not break status/connect


def _indexed_scopes() -> list[dict]:
    """Persisted scopes on disk — a new chat (or restarted daemon) has an
    empty _scopes, but index_root/*.db proves what was connected. Handles
    are reconstructed as scope:{key}:{root} — re-bindable through
    _scope()'s disk-proof path without a live connect (#263)."""
    out = []
    try:
        dbs = sorted((x for x in index_root().glob("*.db")
                      if x.name != "rpc.db"),
                     key=lambda p: -p.stat().st_mtime)
    except OSError:
        return out
    for db in dbs:
        try:
            idx = _index_for(db.stem)
            root = idx.meta_get("root_path")
            if not root:
                continue                    # feedback.db etc — not a scope
            st = idx.stats()
            out.append({"scope_handle": f"scope:{db.stem}:{root}",
                        "root_path": root, "pages": st["pages"],
                        "as_of": st["as_of"], "last_crawl": _last_crawl(idx)})
        except Exception:                    # noqa: BLE001,S112 — corrupt or
            continue                         # mid-write db must not break status
    return out


@tool(mcp)
def status() -> dict:
    """Report setup readiness — no credentials exist for this agent."""
    try:
        profile = LocalProfile.load()
        cfg = {"root": profile.root_path, "device": profile.device}
    except SetupError as e:
        cfg = {"error": str(e)}
    from confluence_knowledge.health import health_checks
    _resume_orphaned_crawls()
    root = index_root()
    checks = health_checks(home(), workdir(), APP_NAME, "local-knowledge",
                           index_root=root)

    if _daemon_on():
        # the http endpoint is the reconnectable transport (#206): the IDE
        # points at the daemon's URL — a dead daemon or stale port means
        # tools go unreachable until doctor self-heals it
        try:
            from confluence_knowledge.registration import daemon_http_status
            checks.append(daemon_http_status("local-knowledge"))
        except Exception:                       # noqa: BLE001,S110 —
            pass                                # status must never fail
    last = None
    try:
        cands = sorted((x for x in root.glob("*.db")
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
        "index_root": str(root),
        # every path that participates in cross-IDE sharing (#257) — diff
        # this block between IDEs when "the same root" answers differently
        "paths": {
            "index_root": str(root),
            "home": str(home()),
            "workdir": str(workdir()),
            "daemon_home": str(daemon_home()),
            "daemon_url": _daemon_url() if _daemon_on() else "",
        },
        "daemon": ("running" if _daemon_on() and _rpc().daemon_alive()
                   else "off" if not _daemon_on() else "starting"),
        # #272: the installed package moved ahead of the code this
        # process loaded — the watcher exits for respawn once idle
        "stale_code": _STALE_CODE,
        "live_scopes": sorted(_scopes),
        # persisted scopes — visible even when _scopes is empty (new
        # chat, stdio process, restarted daemon) so the agent can reuse
        # an existing index instead of asking for a path (#263)
        "indexed_scopes": _indexed_scopes(),
        "last_crawl": last,
    }


@tool(mcp)
def connect(root: str) -> dict:
    """Bind a scope to a file or folder path. No credentials needed — the
    filesystem itself is the ACL (we can only read what the user can)."""
    _resume_orphaned_crawls()
    out = _daemon_call("connect", {"root": root}, 120)
    if out is not None:
        h = out.get("scope_handle")
        if h and h.startswith("scope:"):
            _, key, p = h.split(":", 2)
            with _state_lock:
                _scopes[h] = {"key": key, "root": Path(p)}
        return out
    if _daemon_on():
        # writes may never bypass the daemon (#189 safeguard 2) — a
        # transient spawn/claim gap is a retryable error, never a local
        # crawl on the read-only index
        return {"_error": "daemon unavailable — retry in a few seconds",
                "daemon": "starting"}

    path = resolve_root(root)
    profile = LocalProfile(str(path), socket.gethostname())
    profile.save()          # remember last-used root for status/doctor
    scope_key = profile.scope_key()
    idx = _index_for(scope_key, for_write=True)
    idx.meta_set("root_path", str(path))   # daemon orphan resume (#189)
    st = idx.stats()
    if st["pages"] > 0:
        idx._warm_search_cache()    # first search pays ~0 build (#281)
    covering = None
    if st["pages"] == 0:
        # #240: a folder inside a fully-indexed parent root answers
        # through the ancestor's snapshot — the subtree is a rel-path
        # prefix set, no re-crawl
        def _covers(a: Index) -> int:
            aroot = a.meta_get("root_path")
            if not aroot:
                return 0
            try:
                under = os.path.relpath(str(path), aroot)
            except ValueError:
                return 0               # different drive
            if under == "." or under.startswith(".."):
                return 0               # same root or outside it
            # A's page_ids are relative to A's root.parent and stored
            # POSIX-normalized (_rel) — match that scheme
            return len(a.subtree_pages_prefix(
                os.path.relpath(str(path), Path(aroot).parent)
                .replace("\\", "/")))
        covering = find_covering(index_root(), scope_key, covers=_covers)
        if covering:
            idx.meta_set("alias_scope", covering["key"])
            idx.meta_set("alias_root", str(path))
    handle = f"scope:{scope_key}:{path}"
    s = {"key": scope_key, "root": path}
    if covering:
        aroot = _index_for(covering["key"]).meta_get("root_path")
        s["reads_from"] = covering["key"]
        # subtree prefix in the ancestor's POSIX-normalized rel scheme
        s["subtree_root"] = os.path.relpath(
            str(path), Path(aroot).parent).replace("\\", "/")
    with _state_lock:
        _scopes[handle] = s
    _start_warmup()
    stale = idx.model_id() is not None and idx.model_id() != EMBED_MODEL_ID
    auto = _maybe_auto_reindex(_scopes[handle], idx) if stale else None
    out = {
        "scope_handle": handle,
        "root": str(path),
        "kind": "folder" if path.is_dir() else "file",
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
        out.update({
            "cached": True,
            "cached_pages": covering["pages"],
            "cached_as_of": covering["as_of"] or None,
            "covered_by": _index_for(covering["key"]).meta_get("root_path"),
            "note": "folder is inside an indexed ancestor root — "
                    "answering from the ancestor's index. "
                    "start_crawl(scope_handle) builds a dedicated index "
                    "if you need fresher data."})
    return out


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
    label = {"done": "Indexing finished",
             "error": "Indexing failed",
             "cancelled": "Indexing cancelled",
             "abandoned": "Indexing stopped"}.get(job["state"], "Indexing ended")
    errs = job["errors"]
    body = (f"{n} files indexed" if job["state"] == "done"
            else f"{n} files seen")
    if errs:
        body += f" — {len(errs)} errors, last: {errs[-1][:60]}"
    notify(f"{label} — local-knowledge", body)


def _worker_loop(s: dict, job_id: str) -> None:
    idx = _index_for(s["key"])
    try:
        _start_warmup()
        _models_ready.wait()
        prog = _progress.setdefault(job_id, Progress())
        c = LocalCrawler(idx, s["root"], embedder=_embedder(),
                         sparse=_sparse(),
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
        notify("local-knowledge",
               "index was built with an older embedding model — "
               "re-indexing so search stays compatible")
    if os.environ.get("LOCAL_KNOWLEDGE_AUTO_REINDEX", "1") == "0":
        return None
    job_id = LocalCrawler(idx, s["root"]).start()
    _cancel_flags.pop(job_id, None)
    _progress[job_id] = Progress()
    return "started" if _ensure_worker(s, job_id) else "queued"


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
    """Start the worker unless another process holds the job's lease —
    exactly one worker per job across IDE windows."""
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
    warns = idx.meta_get(f"warns:{job_id}")
    if warns:
        p["warnings"] = json.loads(warns)
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
    """Begin indexing the root (file or folder, recursive) in the background."""
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
    idx = _index_for(s["key"], for_write=True)
    if s.get("reads_from"):
        # #240: materialize — clone the subtree (rel-prefix set) from the
        # ancestor into this scope's own index, then crawl normally;
        # content_hash reuse keeps unchanged embeddings
        anc = _index_for(s["reads_from"])
        idx.import_subtree(
            anc, anc.subtree_pages_prefix(s["subtree_root"]))
        idx.meta_del("alias_scope")
        idx.meta_del("alias_root")
        s.pop("reads_from", None)
        s.pop("subtree_root", None)
    c = LocalCrawler(idx, s["root"])
    job_id = c.start()
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
    """Read crawl progress instantly; resumes a dead worker automatically."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"], for_write=True)
    job = idx.job(job_id)
    if job is None:
        raise AccessError(f"unknown job {job_id}")
    if job["state"] == "running":
        if not _models_ready.is_set():
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
    progress changes, or after `wait_s` (capped at 120). Each poll emits
    an MCP progress notification — a keep-alive that resets the client's
    request timeout without knowing its value; the 25 s default also
    returns well under typical IDE tool timeouts. `changed` tells
    whether anything moved."""
    s = _scope(scope_handle)
    idx = _index_for(s["key"], for_write=True)
    job = idx.job(job_id)
    if job is None:
        raise AccessError(f"unknown job {job_id}")
    if job["state"] == "running":
        if not _models_ready.is_set():
            _start_warmup()
        _resume_worker(s, job_id)
    # 45s ceiling: a longer in-flight request outlives any client request
    # timeout (the field's -32001) — beyond it we return changed:false
    # and the agent re-calls (#206). The wait_s param still controls the
    # intended wait up to the cap.
    wait_s = max(1, min(int(wait_s), 45))
    deadline = time.monotonic() + wait_s
    wall_deadline = time.time() + wait_s + 10
    last_wall = time.time()
    prev = _progress_for(idx, job_id)
    while prev["state"] == "running" and time.monotonic() < deadline \
            and time.time() < wall_deadline:
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
    start_crawl re-runs (unchanged files are reused)."""
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
                    "(unchanged files are reused)"}


def _coverage(idx: Index) -> str:
    job = idx.latest_job()
    if job is None or job["state"] == "abandoned":
        return "unknown"
    if job["state"] == "running":
        return "refresh in progress — answering from the previous snapshot"
    return "partial" if job["errors"] else "complete"


def _pick_scope(scope_handle: str) -> tuple[str | None, dict | None]:
    """Empty handle → resolve to a persisted scope: the single usable
    index wins outright, several → the caller asks which (#263).
    Returns (handle, error) — exactly one is set."""
    if scope_handle:
        return scope_handle, None
    cands = _indexed_scopes()
    usable = [c for c in cands if c["pages"]]
    if len(usable) == 1:
        return usable[0]["scope_handle"], None
    if len(usable) > 1:
        return None, {"evidence": [], "coverage": "ambiguous",
                      "note": "multiple indexes on this machine — which "
                              "subtree should I use? pass the chosen "
                              "scope_handle on the next call",
                      "candidates": [{k: c[k] for k in
                                      ("scope_handle", "root_path",
                                       "pages", "as_of")}
                                     for c in usable]}
    if cands:                       # every scope empty — the freshest is
        return cands[0]["scope_handle"], None   # the best honest report
    return None, {"evidence": [], "coverage": "unconfigured",
                  "note": "no index on this machine yet — connect a "
                          "folder first"}


@tool(mcp, heavy=True)
def search(scope_handle: str = "", question: str = "") -> dict:
    """Hybrid retrieve + local rerank; returns bounded, cited evidence.

    scope_handle is optional: leave it empty and the search uses the
    single existing index (an explicit choice is required only when
    several scopes are indexed).
    """
    scope_handle, err = _pick_scope(scope_handle)
    if err is not None:
        return err
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
                "note": "no files indexed — run a crawl first"}
    if not _models_loaded(10.0):
        return {"evidence": [], "coverage": "loading", "note": LOADING_NOTE}
    if idx.model_id() is not None and idx.model_id() != EMBED_MODEL_ID:
        return {"evidence": [], "coverage": "stale_model",
                "note": "this index was built with an older embedding model "
                        "— run start_crawl once to rebuild it, then search again"}
    emb = _embedder()
    t_ret = time.monotonic()
    ev = retrieve(idx, question, emb, _reranker(), sparse=_sparse(),
                  colbert=_colbert(),
                  allow_chunks=_subtree_allow(s, idx))
    retrieval_ms = round((time.monotonic() - t_ret) * 1000)
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
    idx.persist_evset(set_id, [e["chunk_id"] for e in ev],
                      _evidence_sets[set_id]["epoch"])   # #231
    resp = {
        "evidence_set": set_id,
        # echo the resolved handle — auto-picked scopes (#263) still need
        # it for the get_evidence/verify_citations chain
        "scope_handle": scope_handle,
        "coverage": _coverage(idx),
        "as_of": st["as_of"],
        "retrieval_ms": retrieval_ms,
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
    if _pool.sparse_missing:
        resp["sparse"] = _pool.sparse_missing      # degraded leg (#287)
    if _pool.colbert_missing:
        resp["colbert"] = _pool.colbert_missing    # optional scorer (#329)
    return resp


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
        keep = idx.subtree_pages_prefix(s["subtree_root"])
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
    qvec = _embedder().embed([question])[0]
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
    s = _scope(scope_handle)
    with _state_lock:
        rec = _evidence_sets.get(evidence_set)
    idx = _read_index(s)          # aliased scopes' evsets live in the
                                  # ancestor's meta (#240)
    if rec is None:
        r = idx.evset_rebind(evidence_set)      # #231 daemon restart
        if r:
            rec = {"scope": s["key"], **r}
    if rec is None or rec["scope"] != s["key"]:
        raise SecurityException(
            "unknown or cross-scope evidence set — run search again and cite its evidence_set")
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
    _start_stale_watcher()
    mcp.run()


if __name__ == "__main__":
    main()
