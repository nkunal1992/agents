"""Architectural guard — any @tool that reads process-local state must
consult the daemon FIRST (via _daemon_call) or be explicitly allowlisted
as read-safe. This is the invariant the verify_citations field bug broke:
search ran daemon-side, the registry lived there, and the proxy's copy
was silently empty — every unit test passed anyway because daemon mode
is pinned off in fixtures.

The check is syntactic: within each tool's body, the first reference to
a shared registry must come AFTER a _daemon_call(...) return-guard, or
the tool must appear in LOCAL_SAFE with a justification comment."""

import ast
import inspect
from pathlib import Path

import sharepoint_knowledge.server_sp as srv

# process-local mutable registries — cross-process inconsistent by design
SHARED = {"_evidence_sets", "_progress", "_cancel_flags", "_workers",
          "_feedback", "_indexes", "_clients"}

# tools that legitimately never daemon-route — each entry needs a why
LOCAL_SAFE = {
    "status":          "health surface — reads only durable/blob/RO state",
    "register_token":  "writes the OS keyring — process-independent",
    "get_evidence":    "pure read-only index path",
    "crawl_step":      "progress merges the durable daemon blob — local "
                       "registry is empty in proxy mode by design",
    "wait_for_crawl":  "same — blob-driven, never a local worker",
}


def _tool_bodies(module_path: Path):
    tree = ast.parse(module_path.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            deco = [ast.unparse(d) for d in node.decorator_list]
            if any("tool(" in d for d in deco):
                yield node.name, node


def _first_daemon_call_line(fn) -> int | None:
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and "_daemon_call" in ast.unparse(n.func):
            return n.lineno
    return None


def _first_shared_ref(fn) -> tuple[str, int] | None:
    """First read of a shared registry (call or attribute access)."""
    best = None
    for n in ast.walk(fn):
        name = None
        if isinstance(n, ast.Name) and n.id in SHARED:
            name, line = n.id, n.lineno
        elif isinstance(n, ast.Attribute) and n.attr in SHARED:
            name, line = n.attr, n.lineno
        if name and (best is None or line < best[1]):
            best = (name, line)
    return best


def test_every_tool_satisfies_the_routing_contract():
    path = Path(inspect.getfile(srv))
    failures = []
    for name, fn in _tool_bodies(path):
        ref = _first_shared_ref(fn)
        if ref is None:
            continue                          # pure-RO / no shared state
        if name in LOCAL_SAFE:
            continue                          # documented safe
        dc = _first_daemon_call_line(fn)
        if dc is None or dc > ref[1]:
            failures.append(
                f"{name}: reads {ref[0]} (line {ref[1]}) "
                + ("with no _daemon_call" if dc is None
                   else f"before _daemon_call (line {dc})"))
    assert not failures, (
        "daemon-routing contract violated — these tools consult "
        "process-local state without asking the daemon first:\n  "
        + "\n  ".join(failures))


def test_local_safe_is_still_justified():
    """Allowlist entries must keep explaining themselves — a bare name
    with no reason means the drift-control comment rotted."""
    for name, why in LOCAL_SAFE.items():
        assert why.strip(), f"{name} lost its justification"
