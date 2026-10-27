"""#192: secrets/agent config must be untrackable — .gitignore AND
.git/info/exclude, idempotent, git-optional."""

import subprocess

from confluence_knowledge.settings import ensure_gitignore


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


def _repo(tmp_path):
    r = tmp_path / "proj"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "commit", "-q", "--allow-empty", "-m", "init")
    return r


def test_exclude_layer_blocks_git_add(tmp_path):
    repo = _repo(tmp_path)
    secret = repo / ".conf_auth.json"
    secret.write_text('{"token":"x"}')
    # visible before the guard
    assert ".conf_auth.json" in _git(repo, "status", "--porcelain").stdout
    ensure_gitignore(repo, [".conf_auth.json"], protected=())
    assert ".conf_auth.json" not in _git(repo, "status", "--porcelain").stdout
    # fail-safe: git add . cannot stage it even with .gitignore reverted
    (repo / ".gitignore").write_text("")   # user nuked the visible layer
    _git(repo, "add", ".")
    staged = _git(repo, "diff", "--cached", "--name-only").stdout
    assert ".conf_auth.json" not in staged


def test_exclude_idempotent_and_nested(tmp_path):
    repo = _repo(tmp_path)
    sub = repo / "deep" / "work"
    sub.mkdir(parents=True)
    ensure_gitignore(sub, ["cache.db"], protected=())
    ensure_gitignore(sub, ["cache.db"], protected=())   # no dupes
    ex = repo / ".git" / "info" / "exclude"
    lines = ex.read_text().splitlines()
    assert lines.count("deep/work/cache.db") == 1
    # .gitignore sits beside the workdir, not repo root
    assert (sub / ".gitignore").read_text().strip() == "cache.db"


def test_non_git_dir_still_writes_gitignore(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    ensure_gitignore(d, ["auth.json"], protected=())
    assert (d / ".gitignore").read_text().strip() == "auth.json"
    assert not (d / ".git").exists()


def test_registration_guards_workspace_mcp(tmp_path):
    """register --workspace must git-guard .vscode/mcp.json (#192)."""
    from confluence_knowledge.registration import register
    repo = _repo(tmp_path)
    ws = repo / "ws"
    (ws / ".vscode").mkdir(parents=True)
    out = register(ide="vscode", workspace=ws, workdir=repo / "wd")
    # the user-level target may not exist in tests — find the ws one
    ws_paths = [f for f, _ in out if f.parent == ws / ".vscode"]
    assert ws_paths
    mcp = ws_paths[0]
    assert mcp.exists()
    # excluded — never in git status, add . can't stage it
    assert "mcp.json" not in _git(repo, "status", "--porcelain").stdout
    _git(repo, "add", ".")
    assert not any("mcp.json" in f for f in
                   _git(repo, "diff", "--cached", "--name-only").stdout.split())


def test_git_root_plain_repo(tmp_path):
    from confluence_knowledge.settings import _git_root
    repo = tmp_path / "proj"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    assert _git_root(nested) == (repo.resolve(), repo.resolve() / ".git")


def test_git_root_worktree_pointer(tmp_path):
    from confluence_knowledge.settings import _git_root
    real_gd = tmp_path / "meta" / "worktrees" / "wt1"
    real_gd.mkdir(parents=True)
    wt = tmp_path / "wt-checkout"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {real_gd}\n", encoding="utf-8")
    assert _git_root(wt) == (wt.resolve(), real_gd.resolve())


def test_git_root_no_repo(tmp_path):
    from confluence_knowledge.settings import _git_root
    assert _git_root(tmp_path) is None


def test_run_bounded_hanging_child_returns_none(tmp_path):
    """#322: a child that never exits must NOT hang the caller —
    subprocess.run(timeout) re-communicates post-kill and can block
    forever; the bounded runner abandons instead."""
    import sys
    import time

    from confluence_knowledge.settings import _run_bounded
    t = time.monotonic()
    r = _run_bounded([sys.executable, "-c",
                      "import time; time.sleep(120)"], timeout=0.5)
    assert r is None
    assert time.monotonic() - t < 20    # kill + bounded wait, not forever


def test_run_bounded_normal_child(tmp_path):
    import sys

    from confluence_knowledge.settings import _run_bounded
    r = _run_bounded([sys.executable, "-c", "print('hi')"], timeout=15)
    assert r is not None and r.returncode == 0 and "hi" in r.stdout
