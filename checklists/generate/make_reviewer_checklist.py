#!/usr/bin/env python3
"""Generate PR_Reviewer_Checklist.xlsx — what a reviewer verifies before approving."""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import CellIsRule

wb = Workbook()
ws = wb.active
ws.title = "Reviewer Checklist"

DARK = PatternFill("solid", fgColor="1F4E79")
SECT = PatternFill("solid", fgColor="4472C4")
LIGHT = PatternFill("solid", fgColor="D9E2F3")
WHITE_B = Font(color="FFFFFF", bold=True, size=14)
HDR = Font(bold=True, color="FFFFFF")
BOLD = Font(bold=True)
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")

ws.merge_cells("A1:G1")
ws["A1"] = "PR REVIEWER CHECKLIST — verify before approving"
ws["A1"].font = WHITE_B
ws["A1"].fill = DARK
ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
ws.row_dimensions[1].height = 26

info = [
    ("Reviewer", "", "PR link", "", "Author", ""),
    ("JIRA key", "", "Source branch", "", "Merge target", ""),
    ("Date", "", "Verdict", "", "Result", "☐ Approved   ☐ Changes requested"),
]
for i, row in enumerate(info, start=3):
    for c, v in enumerate(row, start=1):
        cell = ws.cell(row=i, column=c, value=v)
        if c in (1, 3, 5):
            cell.font = BOLD
            cell.fill = LIGHT
        cell.border = BORDER

HEAD = ["#", "Section", "Check", "What to verify", "Evidence / how",
        "Status", "Comments"]
R0 = 7
for c, h in enumerate(HEAD, start=1):
    cell = ws.cell(row=R0, column=c, value=h)
    cell.font = HDR
    cell.fill = DARK
    cell.border = BORDER
    cell.alignment = Alignment(horizontal="center", vertical="center")

ROWS = [
    # ── Section A: process evidence ──
    (1, "Process", "Author checklist complete",
     "Traceability checklist is attached; every row ✅/✍ — no ⚠ or blanks",
     "Reject immediately if incomplete — do not review code against unproven process"),
    (2, "Process", "Merge target",
     "PR actually targets the stated branch; diff is consistent with that target",
     "Wrong-target PRs merge silently — check the target in Bitbucket, not the description"),
    (3, "Process", "Build evidence",
     "Private build ID + SHA inside the PR's commit range; base build ID + SHA present",
     "Spot-check the SHAs in git — don't take the table on faith"),
    (4, "Process", "Binary/XML report",
     "HTML comparison report attached; 'no diff' is proven; 'yes' paths are listed and justified",
     "Open the report — confirm it's the real GUI output with the feature flag disabled"),
    (5, "Process", "MustFix handling",
     "If Must Fix: MustFix_Log.txt update rides in a commit INSIDE this PR",
     "Ask for the commit SHA if not cited — updates on other branches don't count"),
    (6, "Process", "JIRA traceability",
     "Key in commit messages; ticket content actually covers what the diff does",
     "Open the ticket — verify coverage, not just existence"),
    # ── Section B: code review ──
    (7, "Code", "Change intent",
     "Understand what changed and why; it matches the JIRA + PR description",
     "If the PR does more than the ticket says, push back"),
    (8, "Code", "Feature flag gating",
     "New behavior behind the flag, disabled by default; flag-off path unchanged",
     "The binary-diff process assumes flag-off = no behavior change — verify it holds"),
    (9, "Code", "Correctness & error paths",
     "Edge cases handled; errors propagate visibly; no swallowed exceptions",
     "Read the failure paths, not just the happy path"),
    (10, "Code", "Compatibility & efficiency",
     "Backward-compatible configs/APIs/data formats; no obvious perf regression",
     "Watch loops, allocations, and blocking calls in hot paths"),
    (11, "Code", "Your verdict",
     "Approve, or request changes with specific actionable comments",
     "Approval means you co-own the quality — 'looks fine' without reading is a rubber stamp"),
]
for r, row in enumerate(ROWS, start=R0 + 1):
    num, sect, check, verify, how = row
    vals = [num, sect, check, verify, how, "", ""]
    for c, v in enumerate(vals, start=1):
        cell = ws.cell(row=r, column=c, value=v)
        cell.border = BORDER
        cell.alignment = WRAP
        if c == 1:
            cell.alignment = Alignment(horizontal="center", vertical="top")
        if c == 3:
            cell.font = BOLD
        if c == 2:
            cell.fill = SECT if sect == "Process" else PatternFill("solid", fgColor="70AD47")
            cell.font = Font(bold=True, color="FFFFFF")
            cell.alignment = Alignment(horizontal="center", vertical="top")
    ws.row_dimensions[r].height = 44

dv = DataValidation(
    type="list",
    formula1='"✅ Verified,⚠ Issue,— n/a"',
    allow_blank=True,
    showDropDown=False,
)
ws.add_data_validation(dv)
LAST = R0 + len(ROWS)
dv.add(f"F{R0+1}:F{LAST}")
ws.conditional_formatting.add(
    f"F{R0+1}:F{LAST}",
    CellIsRule(operator="equal", formula=['"✅ Verified"'],
               fill=PatternFill("solid", fgColor="C6EFCE"),
               font=Font(color="006100")))
ws.conditional_formatting.add(
    f"F{R0+1}:F{LAST}",
    CellIsRule(operator="equal", formula=['"⚠ Issue"'],
               fill=PatternFill("solid", fgColor="FFC7CE"),
               font=Font(color="9C0006", bold=True)))

ws["B5"].number_format = "dd-mmm-yyyy"

lr = LAST + 2
ws.cell(row=lr, column=2, value="Status legend").font = BOLD
ws.cell(row=lr, column=4, value="✅ Verified — checked yourself, in the artifact/git/ticket — not on the author's word").alignment = WRAP
ws.cell(row=lr + 1, column=4, value="⚠ Issue — comment on the PR and block approval until resolved").alignment = WRAP
ws.cell(row=lr + 2, column=4, value="— n/a — check does not apply to this PR").alignment = WRAP

rr = lr + 4
ws.cell(row=rr, column=2, value="Rules").font = BOLD
rules = [
    "Rows 1–6 are the author's evidence re-verified — a reviewer who only reads code lets process failures slip through.",
    "Open the artifacts yourself: the HTML report, the ticket, the SHAs. Spot-checking beats trusting the table.",
    "Any ⚠ row = request changes, not approval-with-comments.",
    "Approval is a signature — you are attesting both the process and the code.",
]
for i, t in enumerate(rules):
    ws.cell(row=rr + 1 + i, column=2, value="• " + t).alignment = WRAP
    ws.merge_cells(start_row=rr + 1 + i, start_column=2, end_row=rr + 1 + i, end_column=6)

widths = {"A": 4, "B": 10, "C": 26, "D": 44, "E": 52, "F": 14, "G": 28}
for col, w in widths.items():
    ws.column_dimensions[col].width = w
ws.freeze_panes = f"A{R0+1}"
ws.sheet_view.showGridLines = False

from pathlib import Path
out = str(Path(__file__).resolve().parent.parent / "PR_Reviewer_Checklist.xlsx")
wb.save(out)
print("wrote", out)
