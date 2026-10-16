# code-to-requirements (C2R)

Reverse-engineer code into an audit-ready **Functional Requirement Document**
in AsciiDoc. Point it at a starting class/method and the output files the code
produces — it backward-slices from outputs to inputs and writes a spec complete
enough to rebuild the logic without ever seeing the source.

- **Exhaustive**: every branch, negative path, pre-condition, silent input
  (configs, env vars, DB lookups), and sequential override is documented —
  every output field has a rule for every input state.
- **Audit-ready**: formal "shall" language, stable `FLD-xx`/`TRNS-xx` IDs, a
  global data dictionary, cardinality statements, and a Verification appendix
  with synthetic input → expected-output examples.
- **Verified**: before writing, it dry-runs the code against synthetic inputs
  and corrects the spec where the two disagree.
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

Pick **code-to-requirements** in the agent dropdown. It asks for two things:

1. **Starting point** — a class, method, or a plain description of the process.
2. **Target output files** — what the code produces (the sinks the slicing
   starts from).

Or run `/c2r` and answer its prompts.

## The pipeline

```
State check (resume if .c2r_state.json exists)
→ for each pending output field:
    slice from output sink → input discovery (incl. silent inputs)
    → path & state analysis (all branches, negative paths, pre-conditions)
    → sequential lifecycle (first assignment → overrides)
    → input-only abstract transformation
    → verify (synthetic input → dry-run → reconcile)
    → update state + .adoc → checkpoint commit on docs/c2r-requirements
→ final docs/requirements/<name>.adoc
```

## Resuming

State persists in `.c2r_state.json` (work-dir root) with checkpoint commits on
the `docs/c2r-requirements` branch — only state and spec files are ever
committed there, never your source changes. Re-open the agent any time: it
reports progress ("analyzed 5 of 12 output fields") and offers **resume** or
**restart fresh**.

## What you get

An `.adoc` file with:

- **Global Data Dictionary** — every field with `FLD-xx` ID, type, definition.
- **Input Specification** — grouped by source (files / API / config / env / DB),
  each with purpose + validations.
- **Output Specification** — per-output tables (field · Dict ID · type · size ·
  `TRNS-xx` ref) and an explicit cardinality statement per output.
- **Transformation Logic** — ordered `TRNS-xx` steps, shared-logic block with
  contextual overrides, and a dedicated negative-path section.
- **Verification** — synthetic input → expected output per transformation,
  plus discrepancies found and fixed during the dry-run pass.

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
- Large programs: the agent documents at transformation level with `file:line`
  traceability rather than narrating internals.

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
