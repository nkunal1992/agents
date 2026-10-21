"""Filesystem layout for local-knowledge — ~/.local-knowledge, LOCAL_KNOWLEDGE_*.

The engine is confluence_knowledge's settings machinery parametrized for the
local connector; no credentials exist (connect reads only paths).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from confluence_knowledge.settings import (
    SetupError,
    default_home,
    resolve_workdir,
)

APP_NAME = "local-knowledge"
INDEX_DIR_NAME = ".local_knowledge_index"
PROFILE_NAME = "profile.json"


def home() -> Path:
    return default_home("LOCAL_KNOWLEDGE_HOME", ".local-knowledge")


def workdir() -> Path:
    return resolve_workdir("LOCAL_KNOWLEDGE_WORKDIR",
                           "LOCAL_KNOWLEDGE_HOME", ".local-knowledge")


def index_root() -> Path:
    return workdir() / INDEX_DIR_NAME


@dataclass(frozen=True)
class LocalProfile:
    """A non-secret connect profile — just the root path that was indexed."""

    root_path: str      # canonical realpath of the file/folder
    device: str         # machine the path lives on (informational)

    def scope_key(self) -> str:
        raw = f"{self.root_path}|{self.device}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def save(self) -> Path:
        p = home() / PROFILE_NAME
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".tmp.{uuid.uuid4().hex[:8]}")   # per-call —
        # concurrent writers (daemon pool threads / proxy) must never
        # share a tmp name (#189)
        tmp.write_text(json.dumps({
            "root_path": self.root_path, "device": self.device,
        }, indent=2))
        os.replace(tmp, p)
        return p

    @staticmethod
    def load() -> LocalProfile:
        p = home() / PROFILE_NAME
        if not p.exists():
            raise SetupError("no local profile — run `local-knowledge connect` "
                             "with a path via the agent, or `local-knowledge doctor`")
        d = json.loads(p.read_text())
        return LocalProfile(d["root_path"], d.get("device", "local"))
