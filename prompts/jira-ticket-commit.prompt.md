---
name: jira-ticket-commit
description: Create JIRA ticket details from a specific commit or commit range
argument-hint: Commit SHA or range (e.g. abc123 or abc123..HEAD)
agent: jira-ticket-writer
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---

Create JIRA ticket details for this commit or range:

${input:range:Commit SHA or range}

Use `git show <sha>` or `git log`/`git diff` for the range. Confirm the scope
(files changed, +X/−Y), then produce the ticket: suggested fields table,
summary, and wiki-markup description. Text output only — no JIRA calls, no file
changes.
