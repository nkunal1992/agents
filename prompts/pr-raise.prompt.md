---
name: pr-raise
description: Raise a pull request for this branch — collects the build/JIRA/doc traceability checklist first
argument-hint: Optionally name the merge-target branch (e.g. "into release/10.5")
agent: pr-raiser
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "pr-raiser" — also copy agents/pr-raiser.agent.md into .github/agents/.
-->


Raise a pull request for the current branch.

${input:target:Merge-target branch (e.g. release/10.5) — ask me if blank}

Follow the pr-raiser flow: establish the scope vs the merge base, verify the
branch is on top of the target (ask me to rebase first if it isn't), collect
the traceability checklist (private/base builds + commits, binary/XML diff,
MustFix, pct-documentation, JIRA), assess the change read-only, then emit the
PR title and paste-ready description with the process checklist table. Flag
any blockers — do not smooth them over.
