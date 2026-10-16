"""The GET-only wrapper is the core security guarantee — test it hard."""

import httpx
import pytest

from confluence_knowledge.client import (
    RULES_V2,
    AccessError,
    ConfluenceClient,
    SecurityException,
)


@pytest.fixture
def client():
    transport = httpx.MockTransport(lambda req: httpx.Response(
        200, json={"ok": True}))
    return ConfluenceClient(
        "https://acme.atlassian.net", {"Authorization": "Basic x"},
        RULES_V2, session=httpx.Client(transport=transport))


class SpyTransport(httpx.BaseTransport):
    def __init__(self):
        self.calls = []

    def handle_async_request(self, request):
        raise AssertionError("async not used")

    def handle_request(self, request):
        self.calls.append(request)
        return httpx.Response(200, json={"ok": True})


def test_get_allowed(client):
    r = client.get("wiki/api/v2/pages/123")
    assert r.json() == {"ok": True}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE",
                                    "post", "Put", "delete"])
def test_non_get_rejected_before_transport(client, method):
    with pytest.raises(SecurityException, match="strictly read-only"):
        client.request(method, "wiki/api/v2/pages/123")


def test_non_get_never_touches_network():
    spy = SpyTransport()
    c = ConfluenceClient("https://acme.atlassian.net", {}, RULES_V2,
                         session=httpx.Client(transport=spy))
    with pytest.raises(SecurityException):
        c.request("DELETE", "wiki/api/v2/pages/1")
    assert spy.calls == []


def test_disallowed_endpoint_rejected(client):
    with pytest.raises(SecurityException, match="allowed GET list"):
        client.get("wiki/rest/api/space/ENG")       # not in RULES_V2
    with pytest.raises(SecurityException):
        client.get("wiki/api/v2/pages/123/delete")


def test_allowed_endpoints_pass(client):
    for p in ("wiki/api/v2/pages/123",
              "wiki/api/v2/pages/123/direct-children",
              "wiki/api/v2/folders/9/direct-children",
              "wiki/rest/api/user/current"):
        assert client.get(p).json() == {"ok": True}


def test_absolute_url_escape_rejected(client):
    with pytest.raises(SecurityException, match="escapes"):
        client.get("https://evil.example.com/wiki/api/v2/pages/1")
    # same-origin but off the endpoint allowlist — still refused
    with pytest.raises(SecurityException):
        client.get("https://acme.atlassian.net/other/api/v2/pages/1")


def test_traversal_rejected(client):
    with pytest.raises(SecurityException, match="traversal"):
        client.get("wiki/api/v2/../admin")


def test_redirect_refused():
    t = httpx.MockTransport(
        lambda r: httpx.Response(302, headers={"Location": "https://x"}))
    c = ConfluenceClient("https://acme.atlassian.net", {}, RULES_V2,
                         session=httpx.Client(transport=t))
    with pytest.raises(AccessError, match="redirect"):
        c.get("wiki/api/v2/pages/1")


def test_error_status_mapped():
    for code, frag in [(401, "authentication"), (403, "permission"),
                       (404, "not found"), (429, "rate")]:
        t = httpx.MockTransport(lambda r, c=code: httpx.Response(c, json={}))
        c = ConfluenceClient("https://acme.atlassian.net", {}, RULES_V2,
                             session=httpx.Client(transport=t))
        with pytest.raises(AccessError, match=frag):
            c.get("wiki/api/v2/pages/1")


def test_http_base_rejected():
    with pytest.raises(AccessError, match="https"):
        ConfluenceClient("http://conf.acme.corp", {}, RULES_V2)
