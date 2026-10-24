"""GET-only Confluence REST client.

Every transport call passes through request(): a non-GET method is rejected
with SecurityException before any network I/O, and paths are validated against
a per-adapter endpoint allowlist. No convenience method can bypass this.
"""

from __future__ import annotations

import re
import ssl
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx

# Transient handling: honor Retry-After (seconds or HTTP-date), clamped —
# a hostile/buggy server can't park the crawl for an hour. ≤3 retries.
# 5xx and transport errors get the same bounded budget as 429s — a single
# 502 on a children-listing must not silently abandon a subtree (#175).
_RETRY_MAX = 3
_RETRY_AFTER_CAP_S = 60.0
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504, 507, 509, 529})


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 2.0
    try:
        return max(0.5, min(_RETRY_AFTER_CAP_S, float(value)))
    except ValueError:
        pass
    try:
        delta = parsedate_to_datetime(value).timestamp() - time.time()
        return max(0.5, min(_RETRY_AFTER_CAP_S, delta))
    except (TypeError, ValueError):
        return 2.0


def _os_trust_context() -> ssl.SSLContext:
    """SSL context trusting the OS certificate store, not just certifi —
    managed laptops behind TLS-inspecting proxies (Zscaler/Netskope)
    install the corp root into the OS store; httpx's default pins
    certifi.where() and every Confluence call dies with
    CERTIFICATE_VERIFY_FAILED (#260). On Windows, create_default_context
    loads the system store (load_default_certs → ROOT/CA stores); on
    POSIX it honors SSL_CERT_FILE (what pip-system-certs exports for
    requests/urllib/hf_hub anyway)."""
    ctx = ssl.create_default_context()
    try:
        empty = not ctx.get_ca_certs()
    except NotImplementedError:
        empty = False    # truststore injected ctx (pip-system-certs.pth)
    if empty:            # raises — already IS the OS store, no fallback
        import certifi  # exotic env: empty store → certifi still works
        ctx.load_verify_locations(certifi.where())
    return ctx


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
            verify=_os_trust_context(),
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
        url = urljoin(self._base, api_path.lstrip("/"))
        resp = None
        for attempt in range(_RETRY_MAX + 1):
            try:
                resp = self._http.get(url, params=params)
            except httpx.TransportError as e:
                # timeout / conn reset / DNS — transient; same budget
                if attempt == _RETRY_MAX:
                    raise AccessError(
                        f"request failed: {type(e).__name__} — Confluence "
                        "unreachable or timed out") from e
                time.sleep(_retry_after_seconds(None))
                continue
            transient = (resp.status_code == 429
                         or resp.status_code in _TRANSIENT_STATUSES)
            if not transient or attempt == _RETRY_MAX:
                break
            time.sleep(_retry_after_seconds(resp.headers.get("retry-after")))
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
        # retain the validated query — pagination continuation tokens
        # ($skiptoken, cursor) arrive inside absolute next-link URLs (R07)
        return f"{rel}?{u.query}" if u.query else rel

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
