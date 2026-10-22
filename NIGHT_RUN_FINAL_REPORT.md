# Night run — autonomous release report

## Final state — all three packages live on PyPI

| package | version | url | published via |
|---|---|---|---|
| confluence-knowledge | **0.11.0** | https://pypi.org/project/confluence-knowledge/0.11.0/ | stable tag → gate → publish → 3-OS verify |
| local-knowledge | **0.8.0** | https://pypi.org/project/local-knowledge/0.8.0/ | stable tag → gate → publish → 3-OS verify |
| sharepoint-knowledge | **0.8.0** | https://pypi.org/project/sharepoint-knowledge/0.8.0/ | stable tag → gate → publish → 3-OS verify |

Also on the index (RC audit trail): `0.11.0rc1/rc2/rc3`, `0.8.0rc1` ×2.

## Git

- final commit: `5b31732` on `main` — "publish workflow: single-call python
  poll for index propagation"
- tags pushed: `confluence-knowledge-v0.11.0`, `local-knowledge-v0.8.0`,
  `sharepoint-knowledge-v0.8.0` (all on `5b31732`)
- rc tags retained on the same commit: `…rc3`, `…rc1` ×2
- working tree clean; `main` == `origin/main`

## Pipeline outcome (publish-pypi.yml)

| stage | result |
|---|---|
| validate (tag/sha/gate) | PASS — stable tag required green RC on same commit; rc3 provided it |
| build wheels | PASS — `python -m build`, artifacts uploaded |
| test-artifact | PASS — 4 OS×python legs per run |
| e2e-artifact | PASS — wheel e2e on 3 OSes |
| downstream | PASS — local+sharepoint legs install & boot on engine |
| publish | PASS — `pypi.org/project/<pkg>/<ver>/` 200 |
| verify-published | PASS — fresh-install e2e FROM PyPI on 3 OSes per run |

## Smoke test (local, post-publish)

`confluence-knowledge==0.11.0` installs from PyPI in a clean venv;
`confluence-knowledge doctor` runs and reports actionable remediation for
the `[models]` extra (correct — models are the heavy ONNX set).

## Issues encountered and resolved (RELEASE_ISSUES_LOG.md)

| # | failure | fix |
|---|---|---|
| 000 | py311 gate — nested-quote f-string | inner-quote rewrite |
| 001 | `license artifact missing` on fresh CI download — nomic card declares apache-2.0 at line 2605 | `_license_ok` scan window 6KB→256KB |
| 002 | chaos test used chmod-000 (no-op on Windows) | blocking file at the dir path |
| 003 | golden-set honesty 25% — mxbai scores junk 0.02–0.06 vs old 0.02 floor | `MIN_RERANK_SCORE` → 0.1 (real hits ≈0.99) |
| 004 | verify-published raced PyPI index propagation | poll JSON API before e2e |
| 005 | bash poll loop skipped on macOS runner | single python call with internal retry |

## Decisions taken autonomously

- stable tag uses the **package-scoped** name (`confluence-knowledge-v0.11.0`)
  required by `release_gate.py` — the mission's literal `v0.11.0` doesn't
  match the workflow's tag grammar
- `rc1`/`rc2` version names are burned on PyPI (immutable filenames) — each
  post-publish fix advanced the rc number rather than re-uploading
- no local `twine upload` — publish authority lives in the CI pipeline
  (trusted publisher); local smoke verified installability instead

## Evidence artifacts

- `RELEASE_ISSUES_LOG.md` — per-issue root cause + fix
- `PRE_RELEASE_REPORT.md` — the CI-leg gate table
- `REAL_DATA_E2E_TEST_REPORT.md` — scoped real-Cloud proof (100 pages,
  11,731 chunks, canary + citations + feedback all green)
- `eval_report.json` — golden-set: recall 96%, honesty 100%, EVAL OK
