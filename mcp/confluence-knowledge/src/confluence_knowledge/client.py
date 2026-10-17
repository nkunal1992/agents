"""GET-only Confluence REST client.

Every transport call passes through request(): a non-GET method is rejected
with SecurityException before any network I/O, and paths are validated against
a per-adapter endpoint allowlist. No convenience method can bypass this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx


class SecurityException(RuntimeError):
    """Raised when a write/delete/mutation HTTP operation is attempted."""


class AccessError(RuntimeError):
    """Authentication, permission, or reachability failure (actionable)."""


@dataclass(frozen=True)
class EndpointRule:
    pattern: re.Pattern  # matched against the API path
    purpose: str


class ConfluenceClient:
    """GET-only client bound to one site base URL and one route allowlist."""

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        rules: list[EndpointRule],
        timeout: float = 20.0,
        max_bytes: int = 8 * 1024 * 1024,
        session: httpx.Client | None = None,
    ):
        u = urlparse(base_url)
        if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1"):
            raise AccessError("Confluence base URL must be https")
        self._base = base_url.rstrip("/") + "/"
        self._base_origin = f"{u.scheme}://{u.netloc}"
        self._base_path = u.path.rstrip("/")
        self._rules = rules
        self._http = session or httpx.Client(
            headers={"Accept": "application/json", **headers},
            timeout=timeout,
            follow_redirects=False,  # never carry credentials through redirects
        )
        self._max_bytes = max_bytes

    # ---- the GET-only wrapper ----

    def request(self, method: str, path: str, params: dict | None = None) -> httpx.Response:
        if method.upper() != "GET":
            raise SecurityException(
                "Error: This agent is strictly read-only. "
                "Write/Delete operations are forbidden."
            )
        api_path = self._validate_path(path)
        resp = self._http.get(urljoin(self._base, api_path.lstrip("/")), params=params)
        if resp.status_code >= 400:
            raise AccessError(self._describe(resp))
        if resp.status_code >= 300:
            raise AccessError(
                f"unexpected redirect ({resp.status_code}) — check the base URL/context path"
            )
        if len(resp.content) > self._max_bytes:
            raise AccessError(f"response exceeds {self._max_bytes} bytes — refusing to buffer")
        return resp

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        return self.request("GET", path, params)

    # ---- validation ----

    def _validate_path(self, path: str) -> str:
        """Normalize to an API-relative path; enforce site + endpoint bounds."""
        u = urlparse(path)
        if u.scheme or u.netloc:
            # absolute URL (e.g. pagination next-link) must stay inside
            # the configured origin + API base path
            full_base = self._base_origin + self._base_path
            full = f"{u.scheme}://{u.netloc}{u.path}"
            if not (full == full_base or full.startswith(full_base + "/")):
                raise SecurityException(
                    "Error: request escapes the configured Confluence site — refused."
                )
            rel = u.path[len(self._base_path):].lstrip("/")
        elif path.startswith("/"):
            # site-relative path must live under the API base path
            if self._base_path and not path.startswith(self._base_path + "/"):
                raise SecurityException(
                    "Error: request escapes the configured Confluence site — refused."
                )
            rel = path[len(self._base_path):].lstrip("/")
        else:
            rel = path
        if ".." in rel:
            raise SecurityException("Error: path traversal rejected.")
        if not any(r.pattern.match(rel) for r in self._rules):
            raise SecurityException(
                "Error: This agent is strictly read-only. "
                f"Endpoint not on the allowed GET list: {rel}"
            )
        return rel

    @staticmethod
    def _describe(resp: httpx.Response) -> str:
        s = resp.status_code
        return {
            401: "authentication failed (401) — re-run `setup` to rotate "
                 "the credential",
            403: "permission denied (403) — your account can't view this content",
            404: "not found (404) — check the page ID/space and base URL",
            429: "rate limited (429) — retry after the server's Retry-After",
        }.get(s, f"HTTP {s} from Confluence")


# ---- adapters ----

RULES_V2 = [
    EndpointRule(re.compile(r"wiki/api/v2/pages/\d+/?$"), "page body"),
    EndpointRule(re.compile(r"wiki/api/v2/pages/\d+/direct-children/?$"), "children"),
    EndpointRule(re.compile(r"wiki/api/v2/folders/\d+/direct-children/?$"), "folder children"),
    EndpointRule(re.compile(r"wiki/rest/api/user/current/?$"), "identity"),
]

RULES_DC = [
    EndpointRule(re.compile(r"(?:confluence/)?rest/api/content/\d+/?$"), "page body"),
    EndpointRule(re.compile(r"(?:confluence/)?rest/api/content/\d+/child/page/?$"), "children"),
    EndpointRule(re.compile(r"(?:confluence/)?rest/api/user/current/?$"), "identity"),
]

def make_client(profile, token: str, cloud_id: str | None = None) -> ConfluenceClient:
    """Build a client for the configured profile. Token only in headers."""
    import base64

    if profile.env == "cloud":
        auth = base64.b64encode(f"{profile.email}:{token}".encode()).decode()
        # scoped tokens go through the Atlassian gateway bound to one cloudId;
        # the same v2 route shapes apply relative to that base path
        base = (f"https://api.atlassian.com/ex/confluence/{cloud_id}"
                if cloud_id else profile.base_url)
        return ConfluenceClient(base, {"Authorization": f"Basic {auth}"}, RULES_V2)
    return ConfluenceClient(
        profile.base_url, {"Authorization": f"Bearer {token}"}, RULES_DC
    )
