#!/usr/bin/env python3
"""Generate PR_Traceability_Checklist.xlsx — team shareable template."""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import CellIsRule

wb = Workbook()
ws = wb.active
ws.title = "PR Checklist"

DARK = PatternFill("solid", fgColor="1F4E79")
LIGHT = PatternFill("solid", fgColor="D9E2F3")
GATE = PatternFill("solid", fgColor="F2F2F2")
WHITE_B = Font(color="FFFFFF", bold=True, size=14)
HDR = Font(bold=True, color="FFFFFF")
BOLD = Font(bold=True)
THIN = Side(style="thin", color="B0B0B0")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")

# ── Title ─────────────────────────────────────────────────────────────
ws.merge_cells("A1:G1")
ws["A1"] = "PR TRACEABILITY CHECKLIST"
ws["A1"].font = WHITE_B
ws["A1"].fill = DARK
ws["A1"].alignment = Alignment(horizontal="center", vertical="center")
ws.row_dimensions[1].height = 26

# ── Header info block ─────────────────────────────────────────────────
info = [
    ("Author", "", "Source branch", "", "Merge target", ""),
    ("JIRA key", "", "Private build", "", "Base build", ""),
    ("Date", "", "PR link", "", "Result", "☐ Ready   ☐ Blocked"),
]
for i, row in enumerate(info, start=3):
    for c, v in enumerate(row, start=1):
        cell = ws.cell(row=i, column=c, value=v)
        if c in (1, 3, 5):
            cell.font = BOLD
            cell.fill = LIGHT
        cell.border = BORDER

# ── Checklist table ───────────────────────────────────────────────────
HEAD = ["#", "Gate", "What to provide", "Evidence / pass criteria",
        "Your entry", "Status", "Notes"]
R0 = 7
for c, h in enumerate(HEAD, start=1):
    cell = ws.cell(row=R0, column=c, value=h)
    cell.font = HDR
    cell.fill = DARK
    cell.border = BORDER
    cell.alignment = Alignment(horizontal="center", vertical="center")

ROWS = [
    (1, "Merge target", "Branch this PR merges into",
     "Named explicitly — never assumed (no silent main/master)"),
    (2, "Branch on top of target", "Confirm branch is rebased to target tip",
     "git merge-base --is-ancestor <target> HEAD must pass — rebase first if behind"),
    (3, "Private build", "Build ID + the commit SHA it was triggered from",
     "SHA must be a commit inside this PR's range (git log <target>..HEAD) — the wrong-build catch"),
    (4, "Base build", "Build ID + its commit SHA",
     "SHA exists in the repo"),
    (5, "Binary/XML diff (feature flag disabled)", "Yes or No + HTML comparison report (generated via GUI on VDI)",
     "HTML report REQUIRED for both answers — \"no diff\" is the claim that needs proof; if Yes also list differing paths from the report"),
    (6, "Must Fix", "Yes or No",
     "If Yes — MustFix_Log.txt updated by a commit inside this PR (cite the SHA). An update on another branch/repo/uncommitted does NOT count — it must travel with the merge"),
    (7, "pct-documentation", "Commit ID",
     "Commit exists in the pct-documentation repo and documents what changed"),
    (8, "JIRA in commit messages", "Ticket key",
     "Key present in commit messages — record N/M coverage; flag any commit missing a key"),
    (9, "JIRA ticket describes the change", "Ticket link + your written confirmation",
     "You opened the ticket, checked it contains the change details, and wrote your confirmation verbatim (name + date)"),
    (10, "Raise the PR", "PR link",
     "All rows above ✅ — any blank, ⚠, or blocker row means DO NOT raise yet"),
]
for r, row in enumerate(ROWS, start=R0 + 1):
    num, gate, provide, evidence = row
    vals = [num, gate, provide, evidence, "", "", ""]
    for c, v in enumerate(vals, start=1):
        cell = ws.cell(row=r, column=c, value=v)
        cell.border = BORDER
        cell.alignment = WRAP
        if c == 2:
            cell.font = BOLD
        if c in (1,):
            cell.alignment = Alignment(horizontal="center", vertical="top")
    ws.row_dimensions[r].height = 42

# ── Status dropdown + conditional colours ────────────────────────────
dv = DataValidation(
    type="list",
    formula1='"✅ Verified,✍ Attested,⚠ Blocker,— n/a"',
    allow_blank=True,
    showDropDown=False,  # False = show the dropdown arrow (Excel quirk)
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
    CellIsRule(operator="equal", formula=['"✍ Attested"'],
               fill=PatternFill("solid", fgColor="FFEB9C"),
               font=Font(color="9C6500")))
ws.conditional_formatting.add(
    f"F{R0+1}:F{LAST}",
    CellIsRule(operator="equal", formula=['"⚠ Blocker"'],
               fill=PatternFill("solid", fgColor="FFC7CE"),
               font=Font(color="9C0006", bold=True)))

ws["B5"].number_format = "dd-mmm-yyyy"

# ── Legend + rules ────────────────────────────────────────────────────
lr = LAST + 2
ws.cell(row=lr, column=2, value="Status legend").font = BOLD
ws.cell(row=lr, column=3, value="✅ Verified — checked in git / build system").alignment = WRAP
ws.cell(row=lr + 1, column=3, value="✍ Attested — your word, recorded verbatim").alignment = WRAP
ws.cell(row=lr + 2, column=3, value="⚠ Blocker — missing / refused / failed — do not raise").alignment = WRAP
ws.cell(row=lr + 3, column=3, value="— n/a — gate does not apply to this PR").alignment = WRAP

rr = lr + 5
ws.cell(row=rr, column=2, value="Rules").font = BOLD
rules = [
    "Rows 3, 5, 6, 9 catch the real mistakes — wrong build SHA, unproven \"no diff\", MustFix logged elsewhere, unverified JIRA.",
    "Row 5: the HTML comparison report is mandatory even when the answer is \"no diff\".",
    "Row 9: write your confirmation verbatim — it is the traceability record, not a checkbox.",
    "Row 10: any blank / ⚠ row above = do not raise the PR.",
]
for i, t in enumerate(rules):
    ws.cell(row=rr + 1 + i, column=2, value="• " + t).alignment = WRAP
    ws.merge_cells(start_row=rr + 1 + i, start_column=2, end_row=rr + 1 + i, end_column=6)

# ── Layout ────────────────────────────────────────────────────────────
widths = {"A": 4, "B": 30, "C": 36, "D": 56, "E": 26, "F": 15, "G": 26}
for col, w in widths.items():
    ws.column_dimensions[col].width = w
ws.freeze_panes = f"A{R0+1}"
ws.sheet_view.showGridLines = False

from pathlib import Path
out = str(Path(__file__).resolve().parent.parent / "PR_Traceability_Checklist.xlsx")
wb.save(out)
print("wrote", out)
