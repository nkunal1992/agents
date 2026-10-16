---
name: c2r-verify
description: Verification pass — dry-run the code against an existing .adoc spec and reconcile differences
argument-hint: Path to the .adoc file and the code it describes
agent: code-to-requirements
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "code-to-requirements" — also copy agents/code-to-requirements.agent.md into .github/agents/.
-->


Verify this requirement document against the code it describes:

${input:doc:Path to the .adoc file and the starting class/method it documents}

For each transformation (TRNS-xxx), generate a synthetic input, dry-run the
code path (record the file:line path traversed and the actual output in the
evidence log), and reconcile: where code behavior differs from the documented
rule, the requirement is wrong — correct the .adoc and list every discrepancy
found and fixed. Anything untraceable gets `[STATUS: UNRESOLVED]` + a
`blockers[]` entry — never a guess. Check completeness: every output mapped for
every input state, cardinality statements, negative paths. Report results; do
not modify source code.
