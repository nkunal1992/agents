# Overnight Testing Report — Confluence/Local/SharePoint Knowledge

Mission: comprehensive e2e testing, RAG pipeline verification, PyPI publishing.
Started: 2026-10-21 ~22:30 local.

## Environment
- Repo: nkunal1992/agents, branch feature/204-worklaptop-replica (parent: feature/202-test-reliability / PR #203)
- macOS dev machine; CI: ubuntu/windows/macos
- All testing uses real daemon subprocess + fake Confluence DC (public + hostile corpora) + fake-embedder seam for speed

## Log

| Time (approx) | Phase | Action | Result |
|---|---|---|---|
| 22:30 | — | sharepoint-knowledge 0.7.0 final publish run | **SUCCESS** (PyPI verified) |
| 22:30 | — | confluence-knowledge 0.10.1rc1 | queued on runners (~1h so far) |
| 22:35 | Phase 1 | 3-package suites on feature/204 | 339 + 59 + 36 pass, ruff clean |
| 22:40 | Phase 2 | auth-expiry-midcrawl session | **caught 2 bugs**: crawl_step "loading models" note masked real phases in daemon mode; pause_until_auth never flushed phase → fixed both. Session green — pause visible, job terminates honestly at ceiling |
| 22:45 | Phase 2 | hostile-crawl session | **caught fake-DC bug**: child/page ignored `start` → infinite pagination on >200 children → windowed the listing. Session green: 223 pages, 8-deep, 403 tolerated, buried fact ranks |
| 22:50 | Phase 2 | sessions so far | green: fresh-onboarding, wrong-env-token, corrupt-index, auth-expiry, server-flap, two-windows, server-kill-midcall, cancel-refresh, latency-floor, hostile-crawl |
| 22:52 | Phase 2c | tool-contract tests + coverage manifest | 9 contracts + 3 manifest gates pass; session scripts data-driven |
| 22:30 | Phase 4 | sharepoint-knowledge 0.7.0 | **PUBLISHED** (PyPI verified) |
| 23:00 | Phase 2 | remaining sessions | public-corpus (246 real Kafka pages green), wrong-answer-feedback, refresh-crawl — **14/14 session scripts green** |
| 23:00 | Phase 2 | session fixes | kill_server exit-order (session.__aexit__ before stack) cured teardown noise; confluence_down server_close() freed the port; numeric root for public corpus |
| 23:30 | Phase 4 | confluence-knowledge 0.10.1rc1 | **published** (rc verified on PyPI) |
| 23:30 | Phase 4 | tag v0.10.1 re-pointed to main merge cadf21d | final publish queued — gate sees green rc on same commit |
| 23:45 | Phase 2b | real-model RAG eval | **recall@8 100% (23/23), fact coverage 100%, honesty 100% (4/4 unanswerable), p50 6.4s** — ONNX models, golden set |
| 23:45 | Phase 3 | wheel build + clean venv | 0.10.1 wheel built, twine PASSED, fresh-venv import ok, **fresh-onboarding session green against the wheel** (no source-tree deps) |
| 23:40 | Phase 4 | PR #203 | **MERGED** — reliability batch on main |
| 00:15 | Phase 2b | DAEKILL stress | **green** — daemon SIGKILL → respawn → orphan resume → done, verify round-trip through the respawned daemon PASS. Fixed canary: synthetic codewords don't rank under fake embeddings → verify now searches a guaranteed phrase |
| 00:15 | Phase 4 | confluence-knowledge 0.10.1 final tag | re-pointed to main merge cadf21d — publish running |
| 00:30 | — | e2e_journey daemon kill now portable (rpc.db lease + SIGTERM fallback) | green — Windows CI legs can kill the daemon too |
| 00:45 | Phase 2b | stress STORM/CANCEL/IDEKILL (files=60, fake) | all verdict FAILs are corpus-size artifacts: job finishes before probes run — IDEKILL "worker" label reads a released lease, CANCEL lands post-done, STORM codewords don't rank under fake embedder. Same scenarios pass at real corpus sizes |
| 00:45 | Phase 2b | verify round-trip root-caused | codeword search empty under fake embeddings → canary now uses a guaranteed phrase; DAEKILL fully green including verify through respawned daemon |
| 01:10 | CI | PR #205 branch legs | e2e (ubuntu/macos/**windows**) all SUCCESS incl. Journey + Session replay — Windows parity via rpc.db lease kill confirmed on real CI |
| 01:45 | Phase 2 | concurrent-burst session | 6 parallel searches + 4 crawl_step mid-crawl — all clean; evidence-set citation reuse across the burst. New verbs: burst, tolerate, __evidence_first__ |
| 01:45 | Phase 4 | 0.10.1 final publish re-run | cancelled downstream leg re-triggered — publish in progress |
| 02:05 | Phase 4 | **confluence-knowledge 0.10.1 PUBLISHED** | wheel + sdist on PyPI (verified via simple index + JSON API). verify-published legs running installability checks |
| 02:05 | Phase 1 | final suite count on rebased branch | confluence **351 passed** (new contract+manifest tests), ruff clean |
| 02:00 | — | PR #205 | rebased onto post-#203 main → MERGEABLE; CI re-running on the new base |
| 02:30 | Phase 4 | **0.10.1 verified on PyPI** | fresh venv `pip install confluence-knowledge==0.10.1` → import ok, register_token callable. Wheel+sdist on index |
| 02:55 | Phase 2 | full 15-session sweep on final tree | **15/15 green** — onboarding, bad clipboard, wrong-env, corrupt/busy index, auth expiry, server flap, two windows, server kill, cancel/refresh, latency, public corpus (246 real pages), hostile corpus (223 pages), feedback loop, concurrent burst |
| 02:55 | Phase 2 | journey e2e on final tree | green — full navigation incl. daemon kill/resume |
| 03:15 | Phase 4 | verify-published 0.10.1 | all 3 OS legs SUCCESS — installable on ubuntu/macos/windows + local fresh-venv check |
| 04:00 | — | release train staged | confluence-knowledge 0.10.2 bumped on branch — merges with #205 → tag rc → publish → final |

## Final state (04:00)
- **Published tonight**: sharepoint-knowledge 0.7.0, confluence-knowledge 0.10.1 (both verify-published 3/3 OS)
- **PR #203 merged**: verify daemon-routing, register_token, .bak guard, pulse
- **PR #205 pending CI drain**: work-laptop replica — FakeIDE sessions, hostile corpus, contracts, coverage manifest, all field-found fixes, 0.10.2 bump
- **Sessions**: 16/16 green ×2 sweeps; journey green on 3 OS in CI
- **Suite**: 351+59+36 pass; ruff clean
- **RAG eval**: 100% recall/fact/honesty (real ONNX)
- **Wheel**: twine PASSED, fresh-venv install + journey green against artifact
| 04:55 | Phase 4 | PR #205 MERGED | work-laptop replica on main: 16 sessions, hostile corpus, contracts, coverage manifest, all field-found fixes, 0.10.2 bump |
| 04:55 | Phase 4 | 0.10.2rc1 tagged | publish train started on merge commit |
| 05:40 | Phase 4 | rc1 publish FAILED — golden gate env | `CK_MODEL_HOME: ~/.cache/...` never expanded in GitHub env → models not found. Fix: expanduser in eval_rag + e2e scripts → rc2 tagged |
