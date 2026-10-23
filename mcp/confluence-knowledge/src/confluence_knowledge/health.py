"""Machine-readable health checks for doctor/status — detection only.

Every check is {flag, ok, detail, remediation}; the agent turns a red flag
into exactly one fix command (see the agent-file remediation table).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .settings import SetupError

_DEPS = ("onnxruntime", "tokenizers", "fastembed", "huggingface_hub",
         "numpy", "mcp")


def _dep_ok() -> tuple[bool, list[str]]:
    import importlib.util
    missing = [m for m in _DEPS if importlib.util.find_spec(m) is None]
    return not missing, missing


def _models_checks(home: Path, cli: str) -> list[dict]:
    from .retrieval import models_complete
    out = []
    try:
        mc = models_complete(home)
    except SetupError as e:
        return [{"flag": "models_complete", "ok": False, "detail": str(e),
                 "remediation": f"{cli} prepare-models"}]
    for key, spec in mc.items():
        out.append({
            "flag": f"model_{key}", "ok": spec["ok"],
            "detail": ("missing: " + ", ".join(spec["missing"])
                       if spec["missing"] else "present"),
            "remediation": None if spec["ok"] else f"{cli} prepare-models"})
    from .retrieval import active_provider
    out.append({"flag": "onnx_provider", "ok": True,
                "detail": active_provider(), "remediation": None})
    return out


_SERVER_MARKERS = ("confluence_knowledge.server",
                   "local_knowledge.server_local",
                   "sharepoint_knowledge.server_sp")


def _server_process_count() -> int:
    """How many knowledge MCP servers are running — every IDE window
    spawns its own copy of each installed service (~2 GB each once the
    models load), so a count ≫ 3 explains memory pressure. -1 = unknown."""
    import subprocess
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["wmic", "process", "get", "commandline"],
                capture_output=True, text=True, timeout=15,
                check=False).stdout
        else:
            out = subprocess.run(
                ["ps", "-axo", "command"],
                capture_output=True, text=True, timeout=15,
                check=False).stdout
        return sum(1 for line in out.splitlines()
                   if any(m in line for m in _SERVER_MARKERS))
    except Exception:  # noqa: BLE001 — detection only, never fatal
        return -1


def _resources_check(cli: str) -> dict:
    from .resources import _phys_cores, _psutil, limits
    lim = limits()
    n = _server_process_count()
    mem = ""
    free_mb = -1
    if _psutil is not None:
        try:
            free_mb = int(_psutil.virtual_memory().available // 1048576)
        except Exception as e:  # noqa: BLE001 — reading memory is best-effort
            mem = f" free_mem_mb=? ({e})"
    if free_mb >= 0:
        mem = (f" free_mem_mb={free_mb}"
               f" min_free_mb={lim.min_free_mb}"
               + (" LOW" if free_mb < lim.min_free_mb else ""))
    detail = (f"threads={lim.threads} batch_tokens={lim.batch_tokens} "
              f"max_batch={lim.max_batch} "
              f"idle_unload_s={lim.idle_unload_s:.0f} "
              f"fetch_concurrency={lim.fetch_concurrency}"
              f" phys_cores={_phys_cores()}"
              f" guard={'on' if _psutil is not None else 'off (psutil missing)'}"
              f" battery_saver={'on' if lim.on_battery() else 'off'}{mem}"
              + (f"; {n} knowledge server process(es) running"
                 if n >= 0 else "; process count unavailable"))
    too_many = n > 4
    low_mem = _psutil is not None and lim.min_free_mb > 0 and \
        not lim.memory_ok()
    ok = not too_many and not low_mem
    return {"flag": "resources", "ok": ok,
            "detail": detail,
            "remediation": (
                "free RAM is under KNOWLEDGE_MIN_FREE_MB — crawls pause "
                "automatically; close apps to free memory" if low_mem else
                (None if not too_many else
                 "each IDE window starts its own knowledge servers — close "
                 "unused windows (idle servers unload their models after "
                 "~10 min anyway)"))}


def _index_checks(index_root: Path) -> list[dict]:
    """Per-scope index: integrity + stale-model marker. Detection only."""
    from .index import Index
    from .retrieval import MODEL_MARKER as EMBED_MODEL_ID
    if not index_root.exists():
        return []
    out = []
    for db in sorted(index_root.glob("*.db")):
        if db.name == "rpc.db":
            continue                    # daemon control plane, not a scope
        scope = db.stem[:24]
        idx = None
        unreadable = False
        try:
            # read-only + quick_check: status must never take a write
            # lock (a WAL publish by the daemon otherwise stalls this
            # call for seconds — #199) and integrity_check is a full
            # scan per call — quick_check catches real corruption
            idx = Index(db, readonly=True)
            ok = idx.quick_integrity()
            stale = idx.model_id() is not None and \
                idx.model_id() != EMBED_MODEL_ID
        except Exception as e:  # noqa: BLE001 — triage the reason
            # "can't read right now" is NOT corruption — a WAL write or
            # Windows file lock fails the open transiently. Reporting
            # corrupt here sent a field user recrawling a healthy index.
            # Structural damage ("not a database", "malformed") IS
            # corrupt regardless of how it surfaced.
            # walk the cause chain — Index() wraps sqlite errors in
            # SetupError("index unreadable…"); the real reason lives in
            # __cause__/__context__
            msgs = [str(e)]
            for linked in (e.__cause__, e.__context__):
                if linked is not None:
                    msgs.append(str(linked))
            msg = " | ".join(msgs).lower()
            structural = ("not a database" in msg or "malformed" in msg
                          or "corrupt" in msg)
            ok, stale = False, False
            unreadable = not structural
            if structural:
                ok = False
        finally:
            if idx is not None:
                idx.close()  # Windows locks the file while a conn is open
        if unreadable:
            detail = "couldn't read — index busy; retry in a moment"
            remediation = None
        elif not ok:
            detail = "corrupt — delete the index and re-crawl"
            remediation = "start_crawl (rebuilds the index)"
        elif stale:
            detail = "built with an older model — run start_crawl once"
            remediation = "start_crawl (rebuilds the index)"
        else:
            detail = "ok"
            remediation = None
        out.append({
            "flag": f"index:{scope}",
            "ok": ok and not stale and not unreadable,
            "detail": detail,
            "remediation": remediation})
    return out


def health_checks(home: Path, workdir: Path, server_id: str, cli: str,
                  index_root: Path | None = None) -> list[dict]:
    """All non-network checks — the machine-readable half of `status()`."""
    checks = []

    ok, missing = _dep_ok()
    # repair = reinstall the INSTALLED version — never -U (a repair must
    # not silently upgrade into an unverified release)
    inst = None
    if not ok:
        try:
            from importlib.metadata import version as _v
            dist = cli.replace("_", "-")
            inst = _v(dist)
        except Exception:  # noqa: BLE001 — can't pin what isn't installed
            inst = None
    checks.append({"flag": "deps", "ok": ok,
                   "detail": "missing: " + ", ".join(missing) if missing
                             else "all importable",
                   "remediation": None if ok else (
                       f'pip install "{cli}[models]=={inst}"' if inst
                       else f"pip install {cli}[models]")})

    checks += _models_checks(home, cli)

    try:
        from .registration import registered_paths
        regs = registered_paths(server_id=server_id, workspace=workdir)
        ok = bool(regs)
        checks.append({"flag": "registration", "ok": ok,
                       "detail": (", ".join(str(p) for p in regs) if regs
                                  else "no IDE config contains the server"),
                       "remediation": None if ok
                                    else f"{cli} register --ide auto"})
    except Exception as e:  # noqa: BLE001
        checks.append({"flag": "registration", "ok": False,
                       "detail": str(e), "remediation": f"{cli} register"})

    if index_root is not None:
        checks += _index_checks(index_root)

    anchor = home
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    try:
        free = shutil.disk_usage(anchor).free // (1024 * 1024)
    except OSError:
        free = -1  # can't measure — don't flag
    checks.append({"flag": "disk_free", "ok": free < 0 or free >= 2000,
                   "detail": f"{free} MB free",
                   "remediation": None if free >= 2000
                                else "free ~2 GB for the model download"})

    checks.append({"flag": "python", "ok": sys.version_info >= (3, 11),
                   "detail": sys.version.split()[0],
                   "remediation": None if sys.version_info >= (3, 11)
                                else "reinstall with Python >=3.11"})

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    checks.append({"flag": "proxy", "ok": True,
                   "detail": proxy or "none set",
                   "remediation": None})

    checks.append(_resources_check(cli))
    return checks
