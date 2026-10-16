---
name: jira-ticket-since
description: Create JIRA ticket details for everything on top of a base commit — commits + staged + unstaged
argument-hint: Base commit SHA (covers everything after it, including uncommitted work)
agent: jira-ticket-writer
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "jira-ticket-writer" — also copy agents/jira-ticket-writer.agent.md into .github/agents/.
-->


Create JIRA ticket details for everything on top of this base commit —
committed, staged, and unstaged changes included:

${input:base:Base commit SHA}

Use `git diff <base>` so the worktree is included. Confirm the scope (files
changed, +X/−Y), then produce the ticket: suggested fields table, summary, and
wiki-markup description. Text output only — no JIRA calls, no file changes.
