# RELEASE ISSUES LOG — 0.11.0 publish pipeline

Structured record of every failure hit during the RC runs and how each was resolved.

## ISSUE-001 — license gate rejects nomic on fresh CI download

- **Run**: `confluence-knowledge-v0.11.0rc1` (39816764951), `e2e-artifact` +
  `downstream` jobs, step "Warm model cache".
- **Failure**: `SetupError: nomic-ai/nomic-embed-text-v1.5 license artifact
  missing — refusing to ship`.
- **Root cause**: `_license_ok()` scanned only the first **6,000 chars** of
  model cards. `nomic-embed-text-v1.5`'s card puts `license: apache-2.0` at
  **line 2605 of a 72 KB README**, and the repo ships **no standalone
  LICENSE file**. Local passes were silent because the stamp fast-path
  never re-runs the check; CI's fresh download hits it.
- **Fix**: widened the scan window to 256 KB (`retrieval.py::_license_ok`)
  — the whole card is text and cheap to scan. Verified `license_ok=True`
  for nomic, mxbai-xsmall, mxbai-base against the real local store.
- **Regression**: `test_prepare_models` covers the license-path behavior;
  the window change is the minimal-risk fix (no policy relaxation).

## ISSUE-002 — chaos test used POSIX-only permissions

- **Run**: same RC run, `test-artifact (windows-latest, 3.11)`.
- **Failure**: `test_unreadable_index_dir_is_actionable` — `mkdir(mode=0o000)`
  does not block writes on Windows, so `Index()` created the db "silently".
- **Fix**: replaced chmod with a regular file at the directory path —
  blocks `Index()` uniformly on every OS; same "actionable error, not a
  bare traceback" contract.

## ISSUE-000 (pre-pipeline) — py311 f-string gate

- `health.py` used nested same-quote f-string interpolation (PEP 701,
  3.12+) — SyntaxError under py311 wheel tests. Fixed to inner single
  quotes; RC re-tagged.

## ISSUE-003 — golden-set honesty regression under the new reranker

- **Run**: `confluence-knowledge-v0.11.0rc1` (39818616828), step
  "Golden-set RAG gate".
- **Failure**: `honesty: 25% (1/4 unanswerable)` — unanswerable queries
  (wifi password, meaning of life, Japanese weather) returned unrelated
  evidence.
- **Root cause**: `MIN_RERANK_SCORE = 0.02` was calibrated for the old
  stack ("real reranker zeros junk at ~0.00"). `mxbai-rerank-xsmall-v1`
  scores off-topic queries at 0.02–0.056 — above the old floor. Real
  answers score ~0.99. Probed scores: junk band 0.021–0.055, real 0.9925.
- **Fix**: floor raised to `0.1` — ~2× above the measured junk ceiling,
  ~10× below real hits. `LOW_CONFIDENCE=0.3` still flags the 0.1–0.3
  borderline band. Docs-consistency pin updated 0.02→0.1.
- **Verify**: `eval_rag.py` locally → recall 96% (≥80%), honesty 100%
  (4/4). Grounding + docs-consistency tests green (76 passed).

## ISSUE-004 — verify-published raced PyPI index propagation

- **Run**: `confluence-knowledge-v0.11.0rc1` (39823241830), job
  `verify-published`.
- **Failure**: `pip install confluence-knowledge[models]==0.11.0rc1` →
  "No matching distribution found" ~50s after the upload's 200 OK.
- **Root cause**: pypa publish returned success (release IS live at
  pypi.org/project/confluence-knowledge/0.11.0rc1/) but the simple index
  lags the JSON metadata by minutes; verify started instantly.
- **Fix**: verify step now polls the PyPI JSON API for the version to
  appear (12 × 15s bounded) before running the fresh-install e2e, and
  fails clearly if it never does.
- **Note**: `0.11.0rc1` is published and immutable — PyPI rejects
  filename re-uploads, so the corrected RC run uses `rc2`.

## ISSUE-005 — propagation poll failed on CI macOS (loop skipped silently)

- **Run**: rc2 (39827564146), verify-published (macos-latest).
- **Failure**: `never appeared on pypi` ~0.8s into the step despite
  `0.11.0rc2` being live — the bash poll loop produced no iterations,
  then the post-loop check returned immediately.
- **Root cause**: the `for i in $(seq 1 12)` loop + separate post-check
  is fragile across runner environments (empty `$(seq)` output on the
  macOS runner → loop skipped; exact stderr was not captured).
- **Fix**: replaced the bash loop + second check with a single Python
  call carrying its own 24×15s retry — no `seq`, no set -e interplay,
  no split-check race. Verified against live PyPI.

## ISSUE-006 — Windows daemon dies with the agent's console (post-0.11.0 field report)

- **Report**: first real-world run on a Windows laptop — install +
  register + reload OK, then every `status` ✗, doctor's self-heal
  "timed out", agent dead-ended before credentials.
- **Root cause**: `ensure_daemon`/`spawn_watchdog` used
  `start_new_session=True`, which maps to CREATE_NEW_PROCESS_GROUP on
  Windows — the daemon/watchdog stay console-attached and die when the
  spawning shell/IDE terminal closes. `register`'s http URL then
  points at a dead port. Cold-boot imports also exceed the 8 s
  spawn-ready deadlines.
- **Fix**: `detached_spawn_kwargs()` — DETACHED_PROCESS |
  CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB (breakaway retried
  without on denied job objects); deadlines 8→20 s; playbook now has
  the agent read daemon.log/watchdog.log and try `register
  --transport stdio` before dead-ending.
- **Release**: 0.11.1 patch — sisters' `~=0.11.0` picks it up.

## ISSUE-007 — verify-published poll used the JSON API, pip reads the simple index

- **Run**: confluence-knowledge-v0.11.1 stable (39888337997), verify-published
  (windows-latest). `pip install confluence-knowledge==0.11.1` → "No
  matching distribution" ~40s after upload.
- **Root cause**: the ISSUE-005 poll queried `pypi.org/pypi/<pkg>/json`
  which updates before `/simple/<pkg>/` — the index pip actually
  resolves against. The windows leg raced the slower index.
- **Fix**: poll the normalized simple-index page for the dist filename
  (`re.sub` normalization per PEP 503) instead of the JSON API.
- **Also**: the "file yank issue" step lacked `shell: bash` — its
  `\`-continuations hit a PowerShell ParserError on windows runners
  (no issue was filed; the release itself was healthy — ubuntu verify
  passed). Added `shell: bash`.
- **Unrelated flake**: test-artifact windows `test_parallel_is_much_faster`
  (parallel 1.64s vs seq 3.02s) — congested-runner timing assert, rerun
  passed.
