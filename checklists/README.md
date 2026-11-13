# PR process checklists (Excel)

Standalone team artifacts — **not wired to the pr-raiser agent**. Fill one
author checklist per PR; reviewers fill the reviewer checklist. Anything
else in the PR (title, summary, impact) is written by the author.

## Files

| File | Purpose |
|---|---|
| `PR_Traceability_Checklist.xlsx` | **Author template** — 10 gates: merge target, on-top-of-target, private/base builds + SHAs, binary/XML diff (HTML report, FF disabled), MustFix → `MustFix_Log.txt`, pct-documentation commit, JIRA in commits, JIRA content confirmed in writing, raise |
| `PR_Checklist_EXAMPLE_pass.xlsx` | Author worked example — all gates satisfied, ready to raise |
| `PR_Checklist_EXAMPLE_blocked.xlsx` | Author worked example — 4 blockers (build SHA outside range, no report, MustFix on another branch, JIRA unconfirmed) |
| `PR_Reviewer_Checklist.xlsx` | **Reviewer template** — 11 checks in two sections: process evidence re-verified (6) + code review (5) |
| `PR_Reviewer_EXAMPLE_approved.xlsx` | Reviewer worked example — same PR as the author `pass` file |
| `PR_Reviewer_EXAMPLE_changes.xlsx` | Reviewer worked example — same PR as the author `blocked` file |

The example pairs show the same PR from both sides of the table — the
author's claims, and the reviewer's re-verification of them.

## Status columns

- Author: `✅ Verified` (checked in git/build) · `✍ Attested` (your word,
  verbatim) · `⚠ Blocker` (do not raise) · `— n/a`
- Reviewer: `✅ Verified` (you checked the artifact yourself) · `⚠ Issue`
  (request changes) · `— n/a`

Colors apply automatically via conditional formatting.

**pr-raiser integration:** the agent emits a 3-column TSV block after its
markdown checklist — select cell **E8** in the author template and paste to
fill `Your entry / Status / Notes` for all 10 gates in one shot.

## Regenerating

The workbooks are generated with `openpyxl` — tweak a script and rerun:

```bash
pip install openpyxl
python checklists/generate/make_pr_checklist.py        # author template
python checklists/generate/make_pr_examples.py          # author examples
python checklists/generate/make_reviewer_checklist.py   # reviewer template
python checklists/generate/make_reviewer_examples.py    # reviewer examples
```

Outputs are written into this `checklists/` directory.

## The rules the checklists enforce

- Screenshot → **HTML comparison report** (GUI on VDI) — required for *both*
  "diff" and "no diff" answers; "no diff" is the claim that needs proof.
- MustFix → `MustFix_Log.txt` must be touched by a commit **inside this
  PR's range** — updates on other branches/repos don't travel with the merge.
- Private build must be triggered from a commit **inside the PR range** —
  the classic wrong-build catch.
- JIRA confirmation is **written and verbatim** — a checkbox isn't enough.
- Any blank / ⚠ row → do not raise / do not approve.
