"""Filesystem layout and validated settings for confluence-knowledge.

All mutable state lives under the workspace's .conf_knowledge_index/ directory
(or CONF_KNOWLEDGE_HOME). Nothing is written outside declared paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

INDEX_DIR_NAME = ".conf_knowledge_index"
AUTH_FILE_NAME = ".conf_auth.json"
APP_NAME = "confluence-knowledge"


class SetupError(RuntimeError):
    """A problem the user can fix via setup instructions."""


def is_app_control_block(exc: BaseException) -> bool:
    """True when a WDAC/AppLocker policy blocked a native library —
    WinError 4551 "An Application Control policy has blocked this file"
    (unsigned .pyd/.dll such as onnxruntime, tokenizers, sqlite-vec).
    Reinstalling cannot fix it; only an IT allowlist/signer exception
    can. Detectable so we don't emit the misleading "reinstall" advice."""
    if getattr(exc, "winerror", None) == 4551:
        return True
    msg = str(exc).lower()
    return "application control policy" in msg or "blocked by a policy" in msg


def app_control_remediation(what: str) -> str:
    return (f"{what} is blocked by a Windows Application Control "
            "(WDAC/AppLocker) policy — its DLL isn't signed. Ask IT to "
            "allowlist python + site-packages (managed-installer rule) "
            "or run under an approved interpreter; reinstalling won't help.")


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


def reconfigure_console() -> None:
    """cp1252 consoles (default on Western Windows) can't encode →/✗ —
    doctor and update crashed printing them there (#316). Degrade
    unencodable glyphs to '?' rather than raising; streams without a
    reconfigure method (capture buffers, pipes) are left alone."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


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
    existing = set(gi.read_text(encoding="utf-8").splitlines()) if gi.exists() else set()
    missing = [e for e in entries if e not in existing]
    if missing:
        with gi.open("a") as f:
            for e in missing:
                f.write(("\n" if existing else "") + e + "\n")
            existing.update(missing)
    _git_info_exclude(path, entries)
    _assert_not_tracked(path, protected)


def _git_root(path: Path) -> tuple[Path, Path] | None:
    """Nearest repo ancestor as (toplevel, git_dir) — no subprocess.

    `git` can be absent, AppLocker-blocked or hung under emulation, and
    killing its shim leaves a grandchild holding the pipe handles —
    `subprocess.run(timeout=…)` then blocks forever on the post-kill
    re-communicate (#322, seen live on win_arm64). A parent walk covers
    plain repos (`.git` dir) and worktrees (`.git` file → `gitdir:`
    pointer) without touching a shell.
    """
    here = path.resolve()
    for parent in (here, *here.parents):
        marker = parent / ".git"
        try:
            if marker.is_dir():
                return parent, marker
            if marker.is_file():
                for line in marker.read_text(
                        encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("gitdir:"):
                        gd = Path(line[7:].strip())
                        return parent, (gd if gd.is_absolute()
                                        else (parent / gd).resolve())
                return None          # malformed pointer — not a repo
        except OSError:
            return None              # unreadable marker — treat as none
    return None


def _git_info_exclude(path: Path, entries: list[str]) -> None:
    """Mirror ignore entries into .git/info/exclude — the fail-safe layer.

    A no-op outside git repos, and resilient to worktrees (`.git` as a
    file) via the pointer in it — no `git` shell-out (#322). Entries
    are repo-root-relative patterns — a nested workdir's entries carry
    their path prefix.
    """
    found = _git_root(path)
    if found is None:
        return
    top, git_dir = found
    try:
        prefix = path.resolve().relative_to(top)
    except ValueError:
        return                      # path outside the worktree
    rel = []                        # repo-root-relative patterns; git
    for e in entries:               # wants '/' separators everywhere
        pat = e if str(prefix) == "." else str(prefix / e)
        rel.append(pat.replace("\\", "/"))
    ex = git_dir / "info" / "exclude"
    existing = set(ex.read_text(encoding="utf-8").splitlines()) if ex.exists() else set()
    missing = [e for e in rel if e not in existing]
    if missing:
        ex.parent.mkdir(parents=True, exist_ok=True)
        with ex.open("a") as f:
            for e in missing:
                f.write(("\n" if existing else "") + e + "\n")
                existing.add(e)


def _run_bounded(argv: list[str], timeout: float, *, capture: bool = False
                 ) -> subprocess.CompletedProcess | None:
    """subprocess.run(timeout=…) can hang FOREVER after the timeout:
    it kills the child then re-communicates unconditionally — a
    shim-spawned grandchild still holding the pipes (Git-for-Windows
    under ARM64 emulation, #322) makes that wait never return.
    communicate → on expiry kill + bounded wait + abandon. When the
    caller only needs the exit status, capture=False opens no pipes at
    all — a grandchild then has nothing to hold and the kill is clean."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE if capture
                             else subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, text=True,
                             errors="replace")
    except OSError:
        return None                        # absent/blocked binary
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass                           # zombie — abandon it
        return None
    return subprocess.CompletedProcess(argv, p.returncode, out or "", "")


def _assert_not_tracked(workdir: Path, protected: tuple[str, ...]) -> None:
    for rel in protected:
        r = _run_bounded(
            ["git", "-C", str(workdir), "ls-files",
             "--error-unmatch", rel], 10)
        if r is None:
            return  # git absent/blocked/hung — nothing provable
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
        }, indent=2), encoding="utf-8")
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


def shared_home() -> Path:
    """User-level root for cross-service runtime state (#206
    consolidation): one daemon slot per interpreter, one model store.
    Separate from each service's home — credentials and indexes stay
    per-service."""
    env = os.environ.get("KNOWLEDGE_SHARED_HOME")
    return Path(env).expanduser() if env else Path.home() / ".knowledge"


def daemon_home() -> Path:
    """Per-venv daemon slot — same-venv services share ONE daemon process
    (the multi-mount http endpoint); a foreign-venv install can't import
    this venv's modules anyway, so it gets its own slot and port.
    KNOWLEDGE_DAEMON_HOME overrides the slot for tests and power users
    simulating multiple installs from one interpreter (#257)."""
    env = os.environ.get("KNOWLEDGE_DAEMON_HOME")
    if env:
        return Path(env).expanduser()
    import hashlib
    import sys
    key = hashlib.sha256(sys.prefix.encode()).hexdigest()[:10]
    return shared_home() / "daemon" / key
