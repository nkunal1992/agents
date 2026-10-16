---
name: c2r-verify
description: Verification pass — dry-run the code against an existing .adoc spec and reconcile differences
argument-hint: Path to the .adoc file and the code it describes
agent: code-to-requirements
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---

Verify this requirement document against the code it describes:

${input:doc:Path to the .adoc file and the starting class/method it documents}

For each transformation (TRNS-xx), generate a synthetic input, dry-run the code
path, and reconcile: where code behavior differs from the documented rule, the
requirement is wrong — correct the .adoc and list every discrepancy you found
and fixed. Check completeness: every output field mapped for every input state,
cardinality statements present, negative paths covered. Report verification
results; do not modify source code.
