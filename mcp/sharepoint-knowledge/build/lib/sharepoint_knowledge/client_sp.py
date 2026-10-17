"""GET-only Microsoft Graph client for sharepoint-knowledge.

Reuses the allowlist + GET-only machinery of the Confluence client: every
request passes through the same `_validate_path` — the only Graph routes
allowed are the read ones the crawler needs. File downloads use the
pre-authenticated `@microsoft.graph.downloadUrl` fetched WITHOUT the Bearer
header — the token never leaves graph.microsoft.com.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx
from confluence_knowledge.client import (
    AccessError,
    ConfluenceClient,
    EndpointRule,
    SecurityException,
)

GRAPH_RULES = [
    EndpointRule(re.compile(r"sites/[^/]+:.*"), "site by hostname:path"),
    EndpointRule(re.compile(r"sites/[^/]+$"), "site info"),
    EndpointRule(re.compile(r"sites/[^/]+/lists/?$"), "site lists"),
    EndpointRule(re.compile(r"sites/[^/]+/lists/[^/]+/items/?$"), "list items"),
    EndpointRule(re.compile(r"sites/[^/]+/lists/[^/]+/items/[^/]+/?$"), "list item"),
    EndpointRule(re.compile(r"sites/[^/]+/pages/?$"), "site pages"),
    EndpointRule(re.compile(r"sites/[^/]+/pages/[^/]+/?$"), "site page"),
    EndpointRule(re.compile(r"sites/[^/]+/drives/?$"), "document libraries"),
    EndpointRule(re.compile(r"sites/[^/]+/drive/?$"), "default drive"),
    EndpointRule(re.compile(r"sites/[^/]+/drive/root:.+"), "item by path"),
    EndpointRule(re.compile(r"drives/[^/]+/root/children/?$"), "library root"),
    EndpointRule(re.compile(r"drives/[^/]+/items/[^/]+/children/?$"), "folder children"),
    EndpointRule(re.compile(r"drives/[^/]+/items/[^/]+/?$"), "item metadata"),
    EndpointRule(re.compile(r"me/?$"), "identity"),
]


def make_client(base_url: str, token: str) -> ConfluenceClient:
    """Graph client — Bearer stays on the API host only."""
    return ConfluenceClient(base_url, {"Authorization": f"Bearer {token}"},
                            GRAPH_RULES)


def download_file(url: str, max_bytes: int = 25 * 1024 * 1024) -> bytes:
    """Fetch a pre-authenticated downloadUrl — NO Authorization header.

    The URL embeds a short-lived download token; sending our Bearer would
    leak it to a different host (and is unnecessary — these links are
    self-authenticating). https only (localhost allowed for tests).
    """
    u = urlparse(url)
    if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1"):
        raise SecurityException(
            "download URL isn't https — refusing to fetch")
    with httpx.Client(timeout=60.0, follow_redirects=True) as http, \
            http.stream("GET", url) as r:
        # bounded stream — don't buffer an unbounded response
        if r.status_code >= 400:
            raise AccessError(f"download failed: HTTP {r.status_code}")
        out = bytearray()
        for chunk in r.iter_bytes(1 << 20):
            out += chunk
            if len(out) > max_bytes:
                raise AccessError(
                    f"file exceeds {max_bytes} bytes — refusing to buffer")
        return bytes(out)
