"""Self-update check — installed vs latest published version.

One GET to the package index (PyPI or an internal mirror set via
KNOWLEDGE_UPDATE_INDEX) — fails silent to ``latest: None`` when offline or
proxied, and never blocks doctor.
"""

from __future__ import annotations

import json
import os
import urllib.request
from importlib.metadata import PackageNotFoundError, version

INDEX = "https://pypi.org/pypi/{dist}/json"
ENV_INDEX = "KNOWLEDGE_UPDATE_INDEX"
TIMEOUT = 5


def installed_version(dist: str) -> str | None:
    try:
        return version(dist)
    except PackageNotFoundError:
        return None


def latest_version(dist: str, timeout: float = TIMEOUT) -> str | None:
    """Latest version on the index — None when it can't be checked (offline,
    internal mirror without the package, network policy). Never raises."""
    url = os.environ.get(ENV_INDEX, INDEX).format(dist=dist)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())["info"]["version"]
    except Exception:  # noqa: BLE001 — every failure mode means "couldn't check"
        return None


def check(dist: str) -> dict:
    """{installed, latest, update_available} — the dict doctor reports."""
    inst = installed_version(dist)
    latest = latest_version(dist)
    return {
        "installed": inst,
        "latest": latest,
        "update_available": bool(
            inst and latest and _version_tuple(latest) > _version_tuple(inst)),
    }


def _version_tuple(v: str) -> tuple:
    out = []
    for part in v.split("."):
        num = "".join(c for c in part if c.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)
