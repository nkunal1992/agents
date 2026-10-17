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
    return out


def _index_checks(index_root: Path) -> list[dict]:
    """Per-scope index: integrity + stale-model marker. Detection only."""
    from .index import Index
    from .retrieval import EMBED_MODEL_ID
    if not index_root.exists():
        return []
    out = []
    for db in sorted(index_root.glob("*.db")):
        scope = db.stem[:24]
        idx = None
        try:
            idx = Index(db)
            ok = idx.integrity()
            stale = idx.model_id() is not None and \
                idx.model_id() != EMBED_MODEL_ID
        except Exception:  # noqa: BLE001 — unreadable = corrupt
            ok, stale = False, False
        finally:
            if idx is not None:
                idx.db.close()  # Windows locks the file while a conn is open
        out.append({
            "flag": f"index:{scope}", "ok": ok and not stale,
            "detail": ("corrupt — delete the index and re-crawl" if not ok
                       else ("built with an older model — run start_crawl once"
                             if stale else "ok")),
            "remediation": None if ok and not stale
                           else "start_crawl (rebuilds the index)"})
    return out


def health_checks(home: Path, workdir: Path, server_id: str, cli: str,
                  index_root: Path | None = None) -> list[dict]:
    """All non-network checks — the machine-readable half of `status()`."""
    checks = []

    ok, missing = _dep_ok()
    checks.append({"flag": "deps", "ok": ok,
                   "detail": "missing: " + ", ".join(missing) if missing
                             else "all importable",
                   "remediation": None if ok
                                else f"pip install -U {cli}[models]"})

    checks += _models_checks(home, cli)

    try:
        from .registration import registered_paths
        regs = registered_paths(server_id=server_id)
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
    return checks
