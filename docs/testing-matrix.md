# Scenario test matrix

Every reported field scenario and each plausible adjacent one, mapped to
the test that covers it. Two tiers:

- **PR-gated**: unit tests + `KNOWLEDGE_E2E=1` session tests
  (`tests/e2e/sessions/`) run on every pull request.
- **Real-venv tier** (nightly, #271): temp venv, real `pip install` of
  built wheels, live post-boot install while a daemon serves.

Scenario intake rule: every new field report gets a row here before or
while being fixed — the matrix is the durable record of "was this
tested?"

## 1. Install & registration lifecycle

| # | Scenario | Covering test(s) |
|---|----------|------------------|
| I1 | Fresh install → `register` → first reload → tools work | `scripts/e2e_bootstrap.py`; real-venv tier (#271) |
| I2 | Service B installed while daemon serves A (stdio) → tagged call reaches B, not A's handler | `test_multi_service.py` (#264); `test_post_boot_install.py` (session e2e) |
| I3 | Same, http registration → `ensure_mounted` restart | `test_multi_service.py` |
| I4 | Service B installed after `knowledge` entry registered → union grows, zero reload | `test_unified.py` (discovery); `test_unified_session.py` (stdio union); real-venv flagship (#271) |
| I5 | Engine `pip -U` while proxies run → stale proxy code | #272 spike: client respawn check or doctor `stale_proxy` flag |
| I6 | Engine `pip -U` while daemon runs → stale-daemon flag | `test_multi_service.py` (#226) |
| I7 | `pip uninstall` a service → tools vanish next `tools/list` | `test_unified.py`; real-venv (#271) |
| I8 | `register --remove` | `test_registration.py` |
| I9 | Multiple IDEs registered, shared index | `test_stdio_two_installs.py` (#257) |
| I10 | Workspace-scoped mcp.json + gitignore guard | `test_git_exclude.py` |
| I11 | `knowledge` + per-service entries coexisting | `test_unified.py` (dupes harmless, documented) |

## 2. Chat / session lifecycle

| # | Scenario | Covering test(s) |
|---|----------|------------------|
| C1 | New chat, one index, bare question → reuse-first search | server-flow units (#263); `test_reuse_first_session.py` |
| C2 | New chat, N indexes → `ambiguous` + candidates | server-flow units (#263) |
| C3 | No index → `unconfigured` → agent asks for location | server-flow units (#263) |
| C4 | `get_evidence`/`verify_citations` after empty-handle search | `test_reuse_first_session.py` (echoed handle feeds get_evidence) |
| C5 | Process restart → scope rebinds from disk meta | #257 units |
| C6 | Page title ("PL Sequencer") instead of URL | error contract tested; title→pageId resolution filed as #270 |
| C7 | IDE restart mid-crawl → daemon survives, reattach | daemon lifecycle units (#206/#253) |
| C8 | Window reload mid-session → proxy respawn, index persists | `test_stdio_two_installs.py` |

## 3. Daemon

| # | Scenario | Covering test(s) |
|---|----------|------------------|
| D1 | Boot discovers all installed services | `test_multi_service.py` (#206) |
| D2 | Tagged miss → `_remount` hot-mounts post-boot install | `test_multi_service.py` (#264) |
| D3 | Tagged miss never cross-dispatches to another service | `test_multi_service.py` + `test_post_boot_install.py` (clean `not mounted`, not confluence's handler) |
| D4 | Untagged legacy row → single-mount fallback | `test_multi_service.py` (#264) |
| D5 | Kill → watchdog respawn; pile-up prevention | watchdog units (#253/#259); `test_multi_daemon.py` |
| D6 | Version skew flag → doctor restart | `test_multi_service.py` (#226) |
| D7 | Port drift / stale http.json | registration/health units |
| D8 | vec0 on fresh connection (2nd publish) | sqlite-vec units |
| D9 | Job-object kill (Windows) | `test_detached_spawn_kwargs_windows` (#220) |
| D10 | `knowledge` mount present in `/_services` | `test_multi_daemon.py` (expects `knowledge` in the set) |
| D11 | ensure_daemon respawns into the bound home, not the env slot | `test_ensure_daemon_uses_bound_home_not_env` (#273) |

## 4. Managed laptop (shipped 0.13.0 — regression only)

Proxy env / PAC-only / WDAC / loopback EDR / CFA / OneDrive `+O` / temp
profile / LongPaths / job objects / DNS filter — unit coverage +
`scripts/win_managed_sim.py` for the real-device profile (manual run on
the user's work laptop pending).

## 5. IDE-reload elimination matrix

| Trigger | Behaviour |
|---------|-----------|
| First-ever `register` | one reload — the client must see config once (unavoidable, documented) |
| Install a new service | **zero** — unified union + `tools/list_changed` (#265/#269) |
| Engine `pip -U` (proxy code) | #272 spike: self-respawn on version skew, else doctor flag |
| Daemon restart (stdio default) | invisible already |
| Daemon restart (http entry) | session stale → reload; stdio is the default |
| `register --remove` / transport switch | explicit config action — reload expected |
| Service uninstall | **zero** — union shrinks on next list |
| Per-service → unified migration | re-register + one reload, documented dedupe step |
| `.agent.md` edits | IDE-bound, out of scope |
| `*_WORKDIR`/env changes | config edit — reload expected |
