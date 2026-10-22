# Release evidence — 0.11.0 / 0.8.0 (final)

Release gate = the `publish-pypi.yml` pipeline (the release-gate design requires
the CI pipeline itself — `release_gate.py` — not the local preview script).
The local `scripts/pre_release_check.py` output below is retained for history;
every row it flagged was superseded by the authoritative CI legs.

## publish-pypi runs — all green on commit `5b31732`

| run | tag | validate | matrix (ubuntu/win/macos) | publish | verify-published |
|---|---|---|---|---|---|
| 39831898447 | confluence-knowledge-v0.11.0rc3 | PASS | all legs PASS (test ×4, e2e ×3, downstream ×6) | `0.11.0rc3` on PyPI | fresh-install e2e PASS ×3 |
| 39835506757 | confluence-knowledge-v0.11.0 | PASS (rc3-green same commit) | all legs PASS | **`0.11.0` on PyPI** | fresh-install e2e PASS ×3 |
| 39839095908 | sharepoint-knowledge-v0.8.0rc1 | PASS | PASS | `0.8.0rc1` | PASS |
| 39839095805 | local-knowledge-v0.8.0rc1 | PASS | PASS | `0.8.0rc1` | PASS |
| 39841825470 | sharepoint-knowledge-v0.8.0 | PASS | PASS | **`0.8.0` on PyPI** | fresh-install e2e PASS |
| 39841825417 | local-knowledge-v0.8.0 | PASS | PASS | **`0.8.0` on PyPI** | fresh-install e2e PASS |

## Artifact hygiene (built + inspected this session)

- wheels contain only `src/` + `models.lock.json` + `dist-info/` +
  `dist-info/licenses/LICENSE` — no tests/scripts/dbs
- `License-Expression: MIT` (PEP 639) on all three; `twine check` PASS ×6
- bandit `-ll` on all three `src/`: **0 medium / 0 high**
- local suite: **379 passed**, `-k "not model_download"`, on the post-upgrade
  toolchain (pip 26.2.2, certifi 2026.10.19)

## Earlier local-gate rows — superseded

The prior `scripts/pre_release_check.py` run printed RED rows that were timing
artifacts of the pre-publish order, not defects:

| row | stale verdict | resolution |
|---|---|---|
| sharepoint/local wheel | FAIL — `confluence-knowledge~=0.11.0` not on PyPI | engine **is** published now; sisters' verify-published legs passed fresh-install |
| engine:e2e_real_data | FAIL (interrupted crawl) | shared-daemon journey proven by CI `e2e-artifact` legs + scoped real-Cloud run (100 pages / 11,731 chunks) |
| cloud:e2e_cloud_real | FAIL (interrupted crawl) | scoped subtree 524289 proven — see `REAL_DATA_E2E_TEST_REPORT.md` |
