"""Filesystem layout and validated settings for confluence-knowledge.

All mutable state lives under the workspace's .conf_knowledge_index/ directory
(or CONF_KNOWLEDGE_HOME). Nothing is written outside declared paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

INDEX_DIR_NAME = ".conf_knowledge_index"
AUTH_FILE_NAME = ".conf_auth.json"
APP_NAME = "confluence-knowledge"


class SetupError(RuntimeError):
    """A problem the user can fix via setup instructions."""


def default_home(env_var: str = "CONF_KNOWLEDGE_HOME",
                 dir_name: str = ".confluence-knowledge") -> Path:
    env = os.environ.get(env_var)
    if env:
        return Path(env).expanduser().resolve()
    return (Path.home() / dir_name).resolve()


def resolve_workdir(env_var: str = "CONF_KNOWLEDGE_WORKDIR",
                    home_env: str = "CONF_KNOWLEDGE_HOME",
                    home_dir: str = ".confluence-knowledge") -> Path:
    """Folder for the index and the file-fallback credential, shared by the CLI
    and the server. The env var wins; otherwise a per-user folder, never the
    process cwd (IDEs start servers from read-only install dirs)."""
    env = os.environ.get(env_var)
    if env:
        return Path(env).expanduser().resolve()
    return default_home(home_env, home_dir) / "work"


def workspace_index_root(workdir: Path, index_dir: str = INDEX_DIR_NAME) -> Path:
    return workdir / index_dir


def _is_windows() -> bool:
    return os.name == "nt"


def reject_unsafe_path(path: Path) -> Path:
    """Refuse symlinked/reparse destinations so secrets/indices can't leak."""
    resolved = path.expanduser()
    for part in [resolved, *resolved.parents]:
        if part.exists() and part.is_symlink():
            raise SetupError(f"refusing to write through symlink: {part}")
        if _is_windows() and part.exists():
            try:
                if part.stat().st_file_attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                    raise SetupError(f"refusing to write through reparse point: {part}")
            except (OSError, AttributeError):
                pass
    return resolved


def ensure_gitignore(path: Path, entries: list[str],
                     protected: tuple[str, ...] = (AUTH_FILE_NAME,
                                                 INDEX_DIR_NAME)) -> None:
    """Append ignore entries before any secret/cache write. Idempotent.

    Two layers (#192): .gitignore documents intent for anyone cloning;
    .git/info/exclude is the fail-safe — it lives inside .git/, never
    appears in `git status`, and survives the user reverting .gitignore.
    `git add .` cannot stage an excluded path.

    Raises SetupError if a protected path is already tracked by git.
    """
    gi = path / ".gitignore"
    existing = set(gi.read_text().splitlines()) if gi.exists() else set()
    missing = [e for e in entries if e not in existing]
    if missing:
        with gi.open("a") as f:
            for e in missing:
                f.write(("\n" if existing else "") + e + "\n")
            existing.update(missing)
    _git_info_exclude(path, entries)
    _assert_not_tracked(path, protected)


def _git_info_exclude(path: Path, entries: list[str]) -> None:
    """Mirror ignore entries into .git/info/exclude — the fail-safe layer.

    A no-op outside git repos, and resilient to worktrees (`.git` as a
    file) via `git rev-parse`. Entries are repo-root-relative patterns —
    a nested workdir's entries carry their path prefix.
    """
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir",
             "--show-toplevel"],
            check=False, capture_output=True, timeout=10, text=True)
        if r.returncode != 0:
            return
        lines = r.stdout.splitlines()
        git_dir, top = Path(lines[0]), Path(lines[1])
        if not git_dir.is_absolute():
            git_dir = (path / git_dir).resolve()
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return
    try:
        prefix = path.resolve().relative_to(top.resolve())
    except ValueError:
        return                      # path outside the worktree
    rel = []                        # repo-root-relative patterns; git
    for e in entries:               # wants '/' separators everywhere
        pat = e if str(prefix) == "." else str(prefix / e)
        rel.append(pat.replace("\\", "/"))
    ex = git_dir / "info" / "exclude"
    existing = set(ex.read_text().splitlines()) if ex.exists() else set()
    missing = [e for e in rel if e not in existing]
    if missing:
        ex.parent.mkdir(parents=True, exist_ok=True)
        with ex.open("a") as f:
            for e in missing:
                f.write(("\n" if existing else "") + e + "\n")
                existing.add(e)


def _assert_not_tracked(workdir: Path, protected: tuple[str, ...]) -> None:
    import subprocess

    for rel in protected:
        try:
            r = subprocess.run(
                ["git", "-C", str(workdir), "ls-files", "--error-unmatch", rel],
                check=False, capture_output=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return  # not a git repo / git absent — nothing tracked
        if r.returncode == 0:
            raise SetupError(
                f"{rel} is already tracked by git — refusing to write secrets/cache there. "
                f"Remove it from the index first: git rm --cached -r {rel}"
            )


@dataclass(frozen=True)
class SiteProfile:
    """A non-secret connection profile. Tokens live in a credential store."""

    env: str            # "cloud" | "datacenter"
    base_url: str       # canonical https base incl. context path (DC)
    email: str | None   # cloud only
    credential_ref: str # opaque key — not the token itself

    @staticmethod
    def canonical_base(env: str, raw_url: str) -> str:
        u = urlparse(raw_url.strip())
        if u.scheme not in ("https",) and u.hostname not in ("localhost", "127.0.0.1"):
            raise SetupError("Confluence URL must be https (localhost allowed for tests only)")
        if u.username or u.password:
            raise SetupError("credentials must not be embedded in the URL")
        base = f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"
        if env == "cloud" and not re.search(r"\.atlassian\.net$", u.hostname or ""):
            raise SetupError("cloud environment requires an *.atlassian.net site URL")
        return base

    def scope_key(self, principal: str, root_page_id: str) -> str:
        """Partition identity: site + authenticated account + root page."""
        raw = f"{self.base_url}|{principal}|{root_page_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def save(self, home: Path) -> Path:
        p = home / "profile.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "env": self.env, "base_url": self.base_url,
            "email": self.email, "credential_ref": self.credential_ref,
        }, indent=2))
        os.replace(tmp, p)  # atomic
        return p

    @staticmethod
    def load(home: Path) -> SiteProfile:
        p = home / "profile.json"
        if not p.exists():
            raise SetupError("no Confluence profile configured — run `confluence-knowledge setup`")
        d = json.loads(p.read_text())
        return SiteProfile(d["env"], d["base_url"], d.get("email"), d["credential_ref"])


def extract_page_id(root: str) -> str:
    """Accept a numeric ID or a Confluence page URL."""
    if re.fullmatch(r"\d+", root.strip()):
        return root.strip()
    m = re.search(r"pageId=(\d+)", root) or re.search(r"/pages/(\d+)", root)
    if m:
        return m.group(1)
    raise SetupError(
        "can't resolve the root page ID from that URL — provide the numeric pageId "
        "(open the page → Copy Link → look for pageId=…)"
    )
