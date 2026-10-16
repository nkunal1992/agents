---
name: jira-ticket
description: Create JIRA ticket details from the current staged + unstaged changes
argument-hint: Optionally add context (e.g. "this fixes the timeout bug")
agent: jira-ticket-writer
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "jira-ticket-writer" — also copy agents/jira-ticket-writer.agent.md into .github/agents/.
-->


Create JIRA ticket details for my current **staged + unstaged** changes.

${input:context:Optional context — the why behind this change}

Confirm the scope (files changed, +X/−Y), then produce the ticket: suggested
fields table, summary, and wiki-markup description. I want text to paste into
JIRA — do not create anything and do not change any files.
