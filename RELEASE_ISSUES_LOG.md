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
