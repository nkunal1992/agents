#!/usr/bin/env python3
"""Fill the checklist template into two worked examples."""
import datetime as dt
from openpyxl import load_workbook

from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
TEMPLATE = str(HERE / "PR_Traceability_Checklist.xlsx")

# header: (row, col) -> value  — labels live in cols 1/3/5, values in 2/4/6
def fill(ws, header, entries):
    for (r, c), v in header.items():
        ws.cell(row=r, column=c, value=v)
    for r, (entry, status, notes) in entries.items():
        ws.cell(row=r, column=5, value=entry)   # Your entry
        ws.cell(row=r, column=6, value=status)  # Status (drives colour)
        ws.cell(row=r, column=7, value=notes)   # Notes

# rows 8..17 = gates 1..10
PASS_HDR = {
    (3, 2): "J. Smith", (3, 4): "feature/PROJ-1234-pool-timeout",
    (3, 6): "release/10.5",
    (4, 2): "PROJ-1234", (4, 4): "PB-20415", (4, 6): "BB-20410",
    (5, 2): dt.date(2026, 11, 10),
    (5, 4): "https://bitbucket.corp/projects/PLT/repos/svc/pull-requests/42",
    (5, 6): "☑ Ready",
}
PASS = {
    8:  ("release/10.5", "✍ Attested", "named explicitly"),
    9:  ("rebased onto release/10.5 tip 9f8e7d6", "✅ Verified",
         "merge-base --is-ancestor passed"),
    10: ("PB-20415 @ a1b2c3d", "✅ Verified", "sha inside PR range"),
    11: ("BB-20410 @ e4f5g6h", "✅ Verified", "sha exists in repo"),
    12: ("No diff — build-compare-report.html attached", "✍ Attested",
         "report generated via GUI on VDI, attached to PR"),
    13: ("Yes — MustFix_Log.txt in commit f7e8d9a", "✅ Verified",
         "in-range commit"),
    14: ("pct-doc@9a8b7c6", "✍ Attested", "different repo — taken on attestation"),
    15: ("PROJ-1234 (3/3 commits)", "✅ Verified", "full coverage"),
    16: ('"verified — ticket covers pool changes and flag" — J. Smith, 10-Nov',
         "✍ Attested", "written confirmation recorded verbatim"),
    17: ("PR !42 raised", "✅ Verified", "link in header"),
}

BLOCK_HDR = {
    (3, 2): "A. Kumar", (3, 4): "feature/PROJ-2311-cache-warm",
    (3, 6): "release/10.5",
    (4, 2): "PROJ-2311", (4, 4): "PB-20477", (4, 6): "BB-20470",
    (5, 2): dt.date(2026, 11, 12), (5, 4): "— not raised —", (5, 6): "☑ Blocked",
}
BLOCK = {
    8:  ("release/10.5", "✍ Attested", "named explicitly"),
    9:  ("rebased onto release/10.5 tip 9f8e7d6", "✅ Verified", ""),
    10: ("PB-20477 @ 0d3aa91", "⚠ Blocker",
         "sha NOT in PR range — build triggered from an older commit; rebuild from HEAD"),
    11: ("BB-20470 @ b2c4d6e", "✅ Verified", ""),
    12: ('claimed "no diff" but no report attached', "⚠ Blocker",
         "HTML comparison report required for BOTH answers"),
    13: ("Yes — log updated on feature/docs branch", "⚠ Blocker",
         "MustFix_Log.txt must be committed INSIDE this PR — other branches don't count"),
    14: ("pct-doc@77aa12f", "✍ Attested", ""),
    15: ("PROJ-2311 (2/3 commits — c11be2f missing)", "✅ Verified",
         "gap flagged — amend the commit message"),
    16: ("— not confirmed —", "⚠ Blocker",
         "no written confirmation that the ticket describes the change"),
    17: ("DO NOT RAISE — 4 blockers open", "⚠ Blocker", ""),
}

for name, hdr, rows in [
    ("PR_Checklist_EXAMPLE_pass.xlsx", PASS_HDR, PASS),
    ("PR_Checklist_EXAMPLE_blocked.xlsx", BLOCK_HDR, BLOCK),
]:
    wb = load_workbook(TEMPLATE)
    ws = wb.active
    ws.title = "Example — " + ("pass" if "pass" in name else "blocked")
    fill(ws, hdr, rows)
    out = str(HERE / name)
    wb.save(out)
    print("wrote", out)
