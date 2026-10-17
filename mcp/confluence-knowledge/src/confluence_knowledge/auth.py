"""Credential storage: OS keyring → env var → protected file (explicit choice).

Tokens are never accepted via chat, MCP tool arguments, or command-line flags —
the setup wizard prompts with getpass in the user's own terminal.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .settings import APP_NAME, AUTH_FILE_NAME, ensure_gitignore, reject_unsafe_path

SERVICE = APP_NAME
ENV_TOKEN = "CONF_KNOWLEDGE_TOKEN"
ENV_EMAIL = "CONF_KNOWLEDGE_EMAIL"


def _keyring_store(ref: str, secret: str, service: str = SERVICE) -> bool:
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailBackend
        if isinstance(keyring.get_keyring(), FailBackend):
            return False
        keyring.set_password(service, ref, secret)
        return True
    except Exception:  # noqa: BLE001 — keyring backends fail in many ways; fallback is intentional
        return False


def _keyring_get(ref: str, service: str = SERVICE) -> str | None:
    try:
        import keyring
        return keyring.get_password(service, ref)
    except Exception:  # noqa: BLE001 — unavailable keyring → try next provider
        return None


def store_credential(workdir: Path, home: Path, ref: str, token: str,
                     service: str = SERVICE, auth_file: str = AUTH_FILE_NAME,
                     index_dir: str = ".conf_knowledge_index") -> str:
    """Persist token: keyring preferred, protected file as explicit fallback.

    Returns a human description of where it went (never the token).
    """
    if _keyring_store(ref, token, service):
        return "OS credential store"
    # file fallback — gitignore BEFORE writing
    ensure_gitignore(workdir, [auth_file, index_dir + "/"])
    p = reject_unsafe_path(workdir / auth_file)
    data = json.loads(p.read_text()) if p.exists() else {}
    data[ref] = token
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, p)
    if os.name != "nt":
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    return f"protected file {p} (chmod 600)"


def get_credential(workdir: Path, ref: str, env: str = ENV_TOKEN,
                   service: str = SERVICE, auth_file: str = AUTH_FILE_NAME,
                   home_dir: str = ".confluence-knowledge") -> str | None:
    """Lookup order: env var (CI/scripts) → keyring → protected file."""
    t = os.environ.get(env)
    if t:
        return t
    t = _keyring_get(ref, service)
    if t:
        return t
    p = workdir / auth_file
    if p.exists():
        data = json.loads(p.read_text())
        return data.get(ref)
    p2 = Path.home() / home_dir / auth_file
    if p2.exists():
        return json.loads(p2.read_text()).get(ref)
    return None


def redact(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text
