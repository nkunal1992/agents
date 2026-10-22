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
