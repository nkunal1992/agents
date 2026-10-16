---
name: codebase-explain
description: Explain how something works in this repo — end-to-end flow with file:line evidence
argument-hint: What should I explain? (a feature, flow, class, or mechanism)
agent: codebase-guide
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "codebase-guide" — also copy agents/codebase-guide.agent.md into .github/agents/.
-->


Explain this in the context of this repository:

${input:topic:What should I explain?}

Trace the flow end-to-end and answer with your standard format — short answer,
how it works with file:line anchors, key files, gotchas, and confidence.
Read-only: do not change anything.
