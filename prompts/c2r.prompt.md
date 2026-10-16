---
name: c2r
description: Reverse-engineer code into an audit-ready .adoc functional requirement document
argument-hint: Starting class/method and the output file(s) it produces
agent: code-to-requirements
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---

Generate a Functional Requirement Document for:

${input:start:Starting class, method, or process description}

Target output file(s) / sinks:

${input:outputs:Output file(s) the code produces}

Run the full C2R pipeline: check `.c2r_state.json` first (resume if present) →
backward-slice from the outputs → exhaustive input discovery (including silent
inputs: config files, env vars, DB lookups) → map every branch and negative path
→ trace sequential overrides → abstract transformations to input-only rules →
verification loop per field → update `.adoc` + state and checkpoint-commit on
`docs/c2r-requirements` after each output field → deliver
`docs/requirements/<name>.adoc` in formal "shall" language with FLD/TRNS IDs.
Confirm before overwriting; never touch source code.
