#!/usr/bin/env python3
"""Managed-laptop lint gate (#260).

Bans the code patterns that are correct on dev machines but LIE under
managed-Windows restrictions — the failure class behind #259 (watchdog
pile-up: os.kill(pid,0) is CTRL_C_EVENT, not a liveness probe) and #260
(corporate proxies hijacking loopback, removed system tools, blocking
lock calls). Pure text scan — OS-independent, runs on every CI leg.

Matches inside comments/string literals are ignored (docs may quote the
banned pattern — indeed this file does). A deliberately-allowed use is
suppressed with an inline `# managed-ok` marker, same convention as
`nosec`.
"""
from __future__ import annotations

import io
import re
import sys
import tokenize
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC_GLOBS = ("mcp/*/src/**/*.py",)

# (name, regex, allowlisted-path-substrings, why-it-is-banned)
RULES = [
    (
        "os.kill-pid-0",
        re.compile(r"os\.kill\s*\([^,]+,\s*0\b"),
        [],
        ("os.kill(pid, 0) is CTRL_C_EVENT on Windows — it throws on a "
         "detached sibling or TERMINATES it outright (#259). Probe "
         "liveness with psutil.pid_exists + _cmdline_is."),
    ),
    (
        "urlopen-outside-helper",
        re.compile(r"\burlopen\s*\("),
        # update.py checks PyPI — external calls SHOULD honor the proxy.
        # rpc.py hosts _local_urlopen.
        ["update.py", "rpc.py", "check_managed"],
        ("loopback urllib calls must go through _local_urlopen — "
         "urllib.getproxies() reads env AND the Windows registry, so a "
         "corporate proxy can hijack http://127.0.0.1 and self-eject "
         "the daemon (#260)."),
    ),
    (
        "wmic-subprocess",
        re.compile(r"\bwmic\b"),
        ["check_managed"],
        ("wmic is removed from modern Windows 11 and AppLocker-blocked "
         "on managed devices — count/enumerate via psutil instead."),
    ),
    (
        "msvcrt-lk-lock",
        re.compile(r"LK_LOCK\b|msvcrt\.locking"),
        # the ONLY legal msvcrt use is rpc.py's _spawn_lock retry loop
        # (LK_NBLCK + deadline) — anything else blocks or raises.
        ["rpc.py", "check_managed"],
        ("msvcrt.locking(LK_LOCK) retries ~10s then RAISES — a stuck "
         "holder turns every spawn into DaemonUnavailable. Use the "
         "bounded LK_NBLCK retry in rpc._spawn_lock."),
    ),
]


def _opaque_spans(src: str) -> dict[int, list[tuple[int, int]]]:
    """{line: [(col_start, col_end), ...]} for comment/string tokens —
    a match inside one is documentation, not code."""
    spans: dict[int, list[tuple[int, int]]] = {}
    try:
        toks = tokenize.generate_tokens(io.StringIO(src).readline)
        for tok in toks:
            if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                continue
            (srow, scol), (erow, ecol) = tok.start, tok.end
            for row in range(srow, erow + 1):
                lo = scol if row == srow else 0
                hi = ecol if row == erow else 10**9
                spans.setdefault(row, []).append((lo, hi))
    except tokenize.TokenError:
        pass
    return spans


def iter_src():
    for pat in SRC_GLOBS:
        yield from sorted(REPO.glob(pat))


def main() -> int:
    violations = []
    for path in iter_src():
        rel = str(path.relative_to(REPO))
        try:
            src = path.read_text(encoding="utf-8")
        except OSError:
            continue
        opaque = _opaque_spans(src)
        for name, rx, allow, why in RULES:
            if any(a in rel for a in allow):
                continue
            for i, line in enumerate(src.splitlines(), 1):
                if "# managed-ok" in line:
                    continue
                for m in rx.finditer(line):
                    if any(lo <= m.start() < hi
                           for lo, hi in opaque.get(i, ())):
                        continue
                    violations.append(
                        f"{rel}:{i}: [{name}] {line.strip()}\n"
                        f"    → {why}")
    if violations:
        print("managed-laptop lint violations:\n")
        print("\n\n".join(violations))
        return 1
    print(f"check_managed: clean ({sum(1 for _ in iter_src())} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
