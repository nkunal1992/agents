---
name: codebase-where
description: Locate where a behavior or responsibility lives — ranked file:line locations
argument-hint: What are you looking for? (a behavior, handler, validation, call site)
agent: codebase-guide
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "codebase-guide" — also copy agents/codebase-guide.agent.md into .github/agents/.
-->


Locate where this lives in the repository:

${input:topic:What are you looking for?}

Search first, then return a ranked list of `file:line` locations with a one-line
role for each — most relevant first. Note ambiguity or multiple plausible homes.
Keep it concise; offer to explain the mechanism as a follow-up. Read-only.
