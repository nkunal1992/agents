#!/usr/bin/env python3
"""Fill the reviewer checklist into two worked examples."""
import datetime as dt
from openpyxl import load_workbook

from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
TEMPLATE = str(HERE / "PR_Reviewer_Checklist.xlsx")

def fill(ws, header, fills):
    for (r, c), v in header.items():
        ws.cell(row=r, column=c, value=v)
    for r, (status, comment) in fills.items():
        ws.cell(row=r, column=6, value=status)  # Status
        ws.cell(row=r, column=7, value=comment) # Comments

# rows 8..18 = checks 1..11
APPROVE_HDR = {
    (3, 2): "M. Patel",
    (3, 4): "https://bitbucket.corp/projects/PLT/repos/svc/pull-requests/42",
    (3, 6): "J. Smith",
    (4, 2): "PROJ-1234", (4, 4): "feature/PROJ-1234-pool-timeout",
    (4, 6): "release/10.5",
    (5, 2): dt.date(2026, 11, 11), (5, 4): "Approved — no comments outstanding",
    (5, 6): "☑ Approved",
}
APPROVE = {
    8:  ("✅ Verified", "author checklist attached — all rows ✅/✍, none blank"),
    9:  ("✅ Verified", "Bitbucket shows target = release/10.5"),
    10: ("✅ Verified", "PB-20415 @ a1b2c3d — spot-checked, sha is in git log range; base BB-20410 @ e4f5g6h exists"),
    11: ("✅ Verified", "opened build-compare-report.html — 0 binary/XML diffs listed, flag disabled noted in header"),
    12: ("✅ Verified", "f7e8d9a inside PR touches MustFix_Log.txt"),
    13: ("✅ Verified", "PROJ-1234 in 3/3 commits; ticket covers pool changes + flag"),
    14: ("✅ Verified", "diff matches ticket — bounded retry queue + 30s timeout, nothing extra"),
    15: ("✅ Verified", "new path behind pool.v2 flag, default off; flag-off code path untouched"),
    16: ("✅ Verified", "queue-full and timeout paths return typed errors to caller — logged once"),
    17: ("✅ Verified", "configs back-compatible; retry queue is O(1) per checkout"),
    18: ("✅ Verified", "approved"),
}

CHANGES_HDR = {
    (3, 2): "R. Chen",
    (3, 4): "— not raised (pre-PR review) —",
    (3, 6): "A. Kumar",
    (4, 2): "PROJ-2311", (4, 4): "feature/PROJ-2311-cache-warm",
    (4, 6): "release/10.5",
    (5, 2): dt.date(2026, 11, 12), (5, 4): "4 issues — see comments",
    (5, 6): "☑ Changes requested",
}
CHANGES = {
    8:  ("⚠ Issue", "checklist shows 4 ⚠ rows — reviewing anyway to confirm; author should re-run gates"),
    9:  ("✅ Verified", "target correct"),
    10: ("⚠ Issue", "PB-20477 @ 0d3aa91 confirmed NOT in git log range — private build was triggered from an older commit; rebuild from HEAD"),
    11: ("⚠ Issue", "no HTML report attached — 'no diff' is unproven; attach the GUI report"),
    12: ("⚠ Issue", "MustFix_Log.txt update is on feature/docs branch — must be a commit inside THIS PR"),
    13: ("⚠ Issue", "ticket describes the cache work but not the new flag — ticket needs updating"),
    14: ("✅ Verified", "diff itself matches the ticket scope"),
    15: ("✅ Verified", "cache warm-up correctly behind cache.warm flag, off by default"),
    16: ("✅ Verified", "error paths look fine — warm failure degrades to cold path with a warn log"),
    17: ("✅ Verified", "no compat concerns"),
    18: ("⚠ Issue", "changes requested — resolve the 4 blockers and re-submit"),
}

for name, hdr, rows in [
    ("PR_Reviewer_EXAMPLE_approved.xlsx", APPROVE_HDR, APPROVE),
    ("PR_Reviewer_EXAMPLE_changes.xlsx", CHANGES_HDR, CHANGES),
]:
    wb = load_workbook(TEMPLATE)
    ws = wb.active
    ws.title = "Example — " + ("approved" if "approved" in name else "changes req")
    fill(ws, hdr, rows)
    out = str(HERE / name)
    wb.save(out)
    print("wrote", out)
