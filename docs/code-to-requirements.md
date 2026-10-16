# code-to-requirements (C2R)

Reverse-engineer code into an audit-ready **Functional Requirement Document**
in AsciiDoc. Point it at a starting class/method and the output files the code
produces — it backward-slices from outputs to inputs and writes a spec complete
enough to rebuild the logic without ever seeing the source.

- **Exhaustive**: every branch, negative path, pre-condition, silent input
  (configs, env vars, DB lookups), and sequential override is documented —
  every output field has a rule for every input state.
- **Audit-ready**: formal "shall" language; immutable `INP-xxx`/`OUT-xxx`/
  `TRNS-xxx` anchors recorded in state so IDs never change on regeneration;
  every cross-reference is a clickable `xref`; TOC/section numbers/highlighting
  enabled via header attributes.
- **Verified**: before writing, it dry-runs the code against synthetic inputs
  and corrects the spec where the two disagree.
- **Safe**: never guesses — untraceable paths are marked
  `[STATUS: UNRESOLVED]`, logged to `blockers[]` in state, and paused for your
  input. Slicing follows the call graph across module/library boundaries.
- **Resumable**: progress lives in `.c2r_state.json` + checkpoint commits on a
  dedicated `docs/c2r-requirements` branch — stop and restart across sessions
  without losing a single analyzed transformation.
- **Write-disciplined**: writes only `.c2r_state.json` + `docs/requirements/*.adoc`,
  confirms before overwriting, never modifies code, never commits user work.

## Quick start

```bash
scripts/install.sh --only code-to-requirements /path/to/your-repo
# optional, powers the handoff buttons:
scripts/install.sh --only code-to-requirements --only codebase-guide --only test-writer /path/to/your-repo
```

Pick **code-to-requirements** in the agent dropdown. It asks for:

1. **Starting point** — a class, method, or a plain description of the process.
2. **Target output files** — what the code produces (the sinks the slicing
   starts from).
3. **Delivery destination** — where the `.adoc` lands:

| Option | Destination | How it works |
|---|---|---|
| **A. Internal** | `docs/c2r-requirements` branch in the same repo | commit + push each checkpoint |
| **B. External repo** | a Git URL + branch + directory you name | maintains a hidden `.c2r-delivery/` clone, `mkdir -p`s your path, writes **only** there, commit + push per checkpoint |
| **C. Local path** | any absolute directory (non-Git) | overwrites the file each checkpoint |

The choice is recorded in `.c2r_state.json` — resumed sessions reuse it without
asking. The state file itself never leaves your repo; only the `.adoc` travels.
For option B, git credentials must already be configured (SSH/credential
helper) — the agent never asks for or embeds secrets, and tells you instead if
a push fails auth.

Or run `/c2r` and answer its prompts.

## The pipeline

```
State check (resume if .c2r_state.json exists)
→ input discovery + Global Data Dictionary
→ GATE 1 — you approve the foundation
→ first output fully documented → GATE 2 — you approve the Golden Sample
→ for each remaining pending output (autonomous):
    slice → path & state analysis → sequential lifecycle
    → input-only abstract transformation → verify (dry-run + reconcile)
    → update state + .adoc → checkpoint commit on docs/c2r-requirements
→ final docs/requirements/<name>.adoc

GATE 3 is always armed: contradictory code paths or missing input sources
pause the run until you clarify.
```

## Resuming

State persists in `.c2r_state.json` (work-dir root) with checkpoint commits on
the `docs/c2r-requirements` branch — only state and spec files are ever
committed there, never your source changes. Re-open the agent any time: it
reports progress ("analyzed 5 of 12 output fields") and offers **resume** or
**restart fresh**. Approved review gates are stored in the state file — a
resumed session never re-asks a gate you already approved.

## Review gates

The agent stops for your sign-off at three points, so a wrong path never burns
a full run:

- **Gate 1 — Foundation**: after all inputs (including silent ones) and the
  Global Data Dictionary are built. Approve or correct before transformations
  start.
- **Gate 2 — Golden Sample**: after the first output file is fully documented.
  Approve it — it becomes the standard for the remaining outputs, which then
  proceed autonomously.
- **Gate 3 — Ambiguity**: any time the code is contradictory or an input source
  can't be resolved, it stops and asks rather than guessing.

## What you get

An `.adoc` file with:

- **Global Data Dictionary** — every field with an immutable `INP-xxx`/
  `OUT-xxx` ID (tracked in `.c2r_state.json` — never renumbered), type,
  definition.
- **Input Specification** — grouped by source (files / API / config / env / DB),
  each with purpose + validations and a relevance tag: `[ACTIVE]` (drives an
  output) or `[NOT USED IN CURRENT SCOPE]` (in the source but unused — never
  appears in a transformation rule). Per-output Transformation Ref links
  express which inputs drive which outputs.
- **Output Specification** — per-output tables (field · Dict ID · type · size ·
  `TRNS-xx` ref) and an explicit cardinality statement per output.
- **Transformation Logic** — ordered `TRNS-xxx` steps acting as a mapping
  layer: simple mappings inline, complex logic by reference (`as per
  xref:ALGO-001[]`), shared-logic block with contextual overrides, and a
  dedicated negative-path section.
- **Business Algorithms & Complex Procedures** — `ALGO-xxx` blocks for
  multi-step math, loops, and complex conditionals: numbered "shall" steps,
  input-only, referenced by every output that uses them — one place to change
  a business rule. Rules depending on more than three conditional variables
  become **decision tables** (every input combination → result).
- **Structured English** — requirements use only `IF`/`THEN`/`ELSE`/`AND`/`OR`/
  `WHILE`/`REPEAT` — no ambiguous prose; auditors can verify logic directly.
- **Verification** — input → actual-output pairs per transformation. The full
  audit trail (file:line paths traversed) lives in `.c2r_state.json` only —
  the `.adoc` carries **no source references**, keeping it a pure functional
  spec (what the system does, never how the code is written).
- Requirements that couldn't be proven are marked `[STATUS: UNRESOLVED]` with
  the blocking reason tracked in `blockers[]`.

## Follow-ups

- `/c2r-verify` — re-verify an existing `.adoc` against the code (fixes the doc
  where they disagree, lists discrepancies).
- Handoff **Explain this code** → codebase-guide.
- Handoff **Write tests from spec** → test-writer — turns the Verification
  appendix into real tests.

## Notes

- The dry-run is a mental trace of the code path — if your repo has a runnable
  test harness, the agent will suggest using it to confirm.
- `.c2r_state.json` is safe to commit (it's the checkpoint mechanism); add it to
  `.gitignore` on branches where you don't want it.
- Specs are only as complete as the slice; inputs the code never reads won't
  appear. The Coverage section says what wasn't traced.
- Large programs: the agent documents at transformation level; `file:line`
  traceability is recorded in `.c2r_state.json`, never in the document.

## Troubleshooting

| Problem | Fix |
|---|---|
| Agent not in dropdown | File must be in `.github/agents/`; reload the IDE |
| `/c2r*` commands missing | Prompt files live in `.github/prompts/`; JetBrains prompt support is preview |
| Spec missed a config input | Tell it the config file — silent-input discovery reads what the slice touches; obscure wiring may need a pointer |
| Output is huge | Scope the starting point to a single process/flow; split the doc per output |

## Limitations

- Slicing is performed analytically (search + reads), not by a compiler-level
  slicer — dynamic dispatch and reflection are traced best-effort and labeled.
- The document reflects code as of the current commit; regenerate after changes.
