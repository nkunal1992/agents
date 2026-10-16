---
name: architecture-package
description: Map a specific package/module — internal type map + inbound/outbound dependencies
argument-hint: Package or module name (e.g. com.acme.orders or orders/)
agent: architecture-mapper
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---
<!--
INSTALL — copy this file into your repo at .github/prompts/
It runs on agent "architecture-mapper" — also copy agents/architecture-mapper.agent.md into .github/agents/.
-->


Map the architecture of this package/module:

${input:pkg:Package or module name}

Scope it to this package plus its direct collaborators: internal type map (key
classes/interfaces and relations), inbound and outbound dependency edges, and
the key flow through it. Write `docs/architecture/ARCHITECTURE.md` +
`docs/architecture/index.html` for this scope. Confirm before overwriting;
label inferred edges; include coverage & caveats.
