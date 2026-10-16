---
name: codebase-tour
description: Guided tour of this repository or a module — structure, flow, entry points, glossary
argument-hint: Optionally name a module or area to scope the tour
agent: codebase-guide
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "codebase-guide" — also copy agents/codebase-guide.agent.md into .github/agents/.
-->


Give me a guided tour of this repository${input:area:, scoped to: }. Follow your
tour format: module map, layering, entry points, cross-cutting concerns, and a
domain glossary — every claim anchored to file:line. Keep it at map level and
offer per-module deep dives as follow-ups. Read-only: do not change anything.
