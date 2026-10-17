"""Filesystem layout + connection profile for sharepoint-knowledge.

Same machinery as the other connectors: ~/.sharepoint-knowledge home,
SP_KNOWLEDGE_* env overrides, a non-secret profile.json. The token cache
itself is a credential blob in the OS keyring (or protected-file fallback).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from confluence_knowledge.settings import (
    SetupError,
    default_home,
    resolve_workdir,
)

APP_NAME = "sharepoint-knowledge"
INDEX_DIR_NAME = ".sp_knowledge_index"
PROFILE_NAME = "profile.json"
AUTH_FILE_NAME = ".sp_auth.json"
AUTH_ENV = "SP_KNOWLEDGE_TOKEN"


def home() -> Path:
    return default_home("SP_KNOWLEDGE_HOME", ".sharepoint-knowledge")


def workdir() -> Path:
    return resolve_workdir("SP_KNOWLEDGE_WORKDIR",
                           "SP_KNOWLEDGE_HOME", ".sharepoint-knowledge")


def index_root() -> Path:
    return workdir() / INDEX_DIR_NAME


def graph_base() -> str:
    """Microsoft Graph base — overridable for the fake server in e2e tests."""
    return os.environ.get("SP_KNOWLEDGE_GRAPH_BASE",
                          "https://graph.microsoft.com/v1.0")


@dataclass(frozen=True)
class SPProfile:
    """Non-secret connection profile. The token cache is a credential blob."""

    tenant_id: str
    client_id: str
    site_url: str       # canonical https site URL the user indexed
    credential_ref: str # opaque key — not the token

    def scope_key(self, principal: str, root: str) -> str:
        raw = f"{self.site_url}|{principal}|{root}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def save(self) -> Path:
        p = home() / PROFILE_NAME
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "tenant_id": self.tenant_id, "client_id": self.client_id,
            "site_url": self.site_url, "credential_ref": self.credential_ref,
        }, indent=2))
        os.replace(tmp, p)
        return p

    @staticmethod
    def load() -> SPProfile:
        p = home() / PROFILE_NAME
        if not p.exists():
            raise SetupError("no SharePoint profile configured — run "
                             "`sharepoint-knowledge setup`")
        d = json.loads(p.read_text())
        return SPProfile(d["tenant_id"], d["client_id"], d["site_url"],
                         d["credential_ref"])
