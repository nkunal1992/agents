"""Entra auth for sharepoint-knowledge — device-code flow + silent refresh.

The MSAL token cache is stored as a credential blob through the shared
keyring/file machinery (same posture as the Confluence PAT). The access
token itself never enters chat, logs, or command-line arguments.

SP_KNOWLEDGE_TOKEN env var short-circuits everything (tests/CI).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from confluence_knowledge.auth import get_credential, store_credential
from confluence_knowledge.settings import SetupError

SCOPES = ["https://graph.microsoft.com/Sites.Read.All", "offline_access"]
ENV_TOKEN = "SP_KNOWLEDGE_TOKEN"
AUTH_FILE = ".sp_auth.json"
INDEX_DIR = ".sp_knowledge_index"
SERVICE = "sharepoint-knowledge"


def _cache_ref(profile) -> str:
    return f"cache:{profile.credential_ref}"


def _load_msal():
    try:
        import msal
    except ImportError as e:
        raise SetupError(
            "msal isn't installed — reinstall with `pip install "
            "sharepoint-knowledge[models]`") from e
    return msal


def _load_cache(workdir: Path, profile):
    msal = _load_msal()
    blob = get_credential(workdir, _cache_ref(profile), env=ENV_TOKEN,
                          service=SERVICE, auth_file=AUTH_FILE,
                          home_dir=".sharepoint-knowledge")
    if blob:
        try:
            cache = msal.SerializableTokenCache()
            cache.deserialize(json.loads(blob))
            return cache
        except Exception:  # noqa: BLE001 — corrupt cache → re-sign-in
            return None
    return None


def _save_cache(workdir: Path, home_dir: Path, profile, cache) -> str:
    return store_credential(workdir, home_dir, _cache_ref(profile),
                            json.dumps(cache.serialize()),
                            service=SERVICE, auth_file=AUTH_FILE,
                            index_dir=INDEX_DIR)


def _app(profile, cache=None):
    msal = _load_msal()
    kwargs = {"token_cache": cache} if cache else {}
    return msal.PublicClientApplication(
        profile.client_id,
        authority=f"{_get_auth_base()}/{profile.tenant_id}",
        **kwargs)


def _get_auth_base() -> str:
    """Authority base override for e2e (fake token endpoint)."""
    return os.environ.get("SP_KNOWLEDGE_AUTH_BASE",
                          "https://login.microsoftonline.com")


def acquire_token(profile, workdir: Path, home_dir: Path) -> str:
    """Silent first (cached refresh token); device flow only on expiry —
    so normal use needs no re-sign-in after setup."""
    if os.environ.get(ENV_TOKEN):
        return os.environ[ENV_TOKEN]
    cache = _load_cache(workdir, profile)
    app = _app(profile, cache)
    accounts = app.get_accounts()
    if accounts:
        r = app.acquire_token_silent(SCOPES, account=accounts[0])
        if r and "access_token" in r:
            if cache and cache.has_state_changed:
                _save_cache(workdir, home_dir, profile, cache)
            return r["access_token"]
    raise SetupError(
        "SharePoint sign-in expired — run `sharepoint-knowledge setup` to "
        "re-authenticate (device code, one browser step)")


def device_sign_in(profile, workdir: Path, home_dir: Path) -> dict:
    """Interactive device-code sign-in — used by `setup`. Prints the
    message Microsoft returns (URL + code); blocks until approved or it
    times out. Returns the stored-cache location description."""
    msal = _load_msal()
    cache = msal.SerializableTokenCache()
    app = _app(profile, cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise SetupError(
            "couldn't start device sign-in: "
            f"{flow.get('error_description', flow)}")
    print("\n" + flow["message"] + "\n")  # "go to microsoft.com/devicelogin…"
    r = app.acquire_token_by_device_flow(flow)
    if "access_token" not in r:
        raise SetupError(
            "sign-in failed: "
            f"{r.get('error_description', r.get('error', r))}")
    where = _save_cache(workdir, home_dir, profile, cache)
    account = r.get("id_token_claims", {})
    return {"where": where,
            "user": (account.get("preferred_username")
                     or account.get("email") or "signed in")}
