"""Three-tier crawl reporting (#187) — shared by all three services.

Tier 1 — `pulse`: a one-line live string the agent can relay verbatim
    ("indexing 7813/~9901 pages | 244 ppm | 1656 MB RSS").
Tier 2 — `stages`: structured discovery/fetch/embed breakdown with
    overall percent and ETA, for clients that render progress UI.
Tier 3 — `audit`: the terminal crawl summary — indexed count, errors
    grouped by cause (HTTP code for the remote services; error class for
    local files), publish accounting, and on-disk index size.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .index import Index

_HTTP_CODE = re.compile(r"HTTP (\d{3})")


def pulse(progress: dict[str, Any], rss_mb: float | None = None) -> str:
    """Tier 1 — compact live line: phase | done/total | rate | RSS."""
    phase = progress.get("phase") or progress.get("state") or "idle"
    done = progress.get("pages_done") or 0
    seen = progress.get("pages_seen") or 0
    remaining = progress.get("remaining") or 0
    total = max(done, seen) + remaining
    parts = [str(phase), f"{done} of ~{total} pages"]
    rate = progress.get("rate_ppm")
    if rate:
        parts.append(f"~{rate}/min")
    errs = progress.get("errors_n") or 0
    if errs:
        parts.append(f"{errs} couldn't be read")
    return " · ".join(parts)


def stages(progress: dict[str, Any]) -> dict[str, Any]:
    """Tier 2 — structured stage breakdown.

    fetch → pages_seen (bodies/reads completed); embed → pages_done
    (rows staged); publish happens once at the end. Totals are estimates
    — discovery expands the queue as it walks, so `total` moves.
    """
    seen = progress.get("pages_seen") or 0
    done = progress.get("pages_done") or 0
    remaining = progress.get("remaining") or 0
    total = max(seen + remaining, 1)
    return {
        "fetch": {"pages": seen,
                  "pct": round(100.0 * seen / total, 1)},
        "embed": {"pages": done,
                  "pct": round(100.0 * done / total, 1)},
        "remaining": remaining,
        "pct_complete": round(100.0 * done / total, 1),
        "eta_s": progress.get("eta_s"),
    }


def _classify_error(err: str) -> str:
    """Group errors so 'Access Denied' vs 'server instability' vs
    'local unreadable' are distinguishable."""
    m = _HTTP_CODE.search(err)
    if m:
        code = m.group(1)
        if code in ("401", "403"):
            return f"access denied (HTTP {code})"
        if code == "429":
            return "rate limited (HTTP 429)"
        return f"server error (HTTP {code})"
    low = err.lower()
    for key, label in (
        ("permission denied", "permission denied"),
        ("unreadable", "unreadable"),
        ("timeout", "timeout"),
        ("timed out", "timeout"),
        ("symlink", "skipped — symlink"),
        ("secret", "skipped — secret-looking"),
        ("too large", "skipped — oversized"),
        ("model changed", "model migration"),
        ("cap reached", "cap reached"),
    ):
        if key in low:
            return label
    return "other"


def index_size_mb(index: Index) -> float | None:
    """On-disk size of the index's main db file."""
    try:
        rows = index.db.execute("PRAGMA database_list").fetchall()
        main = next((r[2] for r in rows if r[1] == "main"), None)
        if not main:
            return None
        import os as _os
        return round(_os.path.getsize(main) / 1048576, 1)
    except Exception:  # noqa: BLE001 — reporting must never fail
        return None


def audit(index: Index, job_id: str) -> dict[str, Any]:
    """Tier 3 — the terminal Crawl Summary Report."""
    job = index.job(job_id)
    if job is None:
        return {}
    groups: dict[str, list[str]] = {}
    for e in job["errors"]:
        groups.setdefault(_classify_error(e), []).append(e)
    pub = index.meta_get(f"publish:{job_id}")
    size = index_size_mb(index)
    out: dict[str, Any] = {
        "job_id": job_id,
        "state": job["state"],
        "pages_indexed": len(index.active_pages()),
        "errors_total": len(job["errors"]),
        "errors_by_cause": {k: len(v) for k, v in sorted(groups.items())},
        "error_examples": {k: v[:3] for k, v in sorted(groups.items())},
    }
    if pub:
        import json as _json
        out["publish"] = _json.loads(pub)   # staged/deleted/kept
    if size is not None:
        out["index_size_mb"] = size
    return out


def current_rss_mb() -> float | None:
    """Own-process RSS for the pulse line — None when psutil is absent."""
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1048576, 1)
    except Exception:  # noqa: BLE001 — reporting must never fail
        return None
