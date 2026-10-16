---
name: architecture
description: Generate the high-level architecture map — ARCHITECTURE.md + explainer site — for the repo or a named scope
argument-hint: repo or a package/module name (e.g. com.acme.orders)
agent: architecture-mapper
model: ["GPT-6 Luna", "GPT-6 Luna (copilot)", "GPT-5 mini", "GPT-4.1"]
---

Generate the high-level architecture map for this scope:

${input:scope:repo}

Run the full pipeline: bounded structure-first exploration → mermaid module and
flow diagrams → `docs/architecture/ARCHITECTURE.md` + `docs/architecture/index.html`.
Confirm with me before overwriting existing files. Everything must be
evidence-based — label inferred edges, and end with coverage & caveats.
