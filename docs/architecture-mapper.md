# architecture-mapper

Generate a visual architecture map of your repo — or a single package — and get
two shareable artifacts: an `ARCHITECTURE.md` with mermaid diagrams, and a
self-contained HTML explainer page for non-engineers.

- **Evidence-based**: every box and arrow comes from build files, imports, or
  config. Inferred relationships are labeled as such; the coverage section says
  what wasn't traced.
- **Two audiences**: engineers get module maps and sequence flows; PMs and new
  joiners get a plain-language explainer page.
- **Write-disciplined**: only writes inside `docs/architecture/`, asks before
  overwriting, and stamps each file with the date and commit it was generated
  from.

## Quick start

```bash
scripts/install.sh --only architecture-mapper /path/to/your-repo
# optional, powers the handoff buttons:
scripts/install.sh --only architecture-mapper --only codebase-guide --only java-modernizer /path/to/your-repo
```

Pick **architecture-mapper** in the agent dropdown, then:

| You type / run | Result |
|---|---|
| `repo` or `/architecture` | Full-repo map: context + module diagrams, flows, glossary |
| `com.acme.orders` or `/architecture-package com.acme.orders` | Package-scope map: internal types + inbound/outbound edges |

Both write `docs/architecture/ARCHITECTURE.md` and `docs/architecture/index.html`.

## What you get

### ARCHITECTURE.md

Generated stamp, plain-language overview, system-context diagram, layered module
map (`flowchart` with subgraphs), modules table, 1–2 key-flow sequence diagrams,
external-systems table, entry points with `file:line`, glossary, and a
**Coverage & caveats** section (proven vs inferred, exclusions, staleness note).

### index.html — the explainer site

A single file with everything inline — no build step, no framework. Hero
("What is this system?" in 3 sentences anyone can read), a legend for reading
the diagrams, one section per diagram with a plain-language walkthrough,
glossary, and the generated stamp.

## Sharing it

- **Commit it** — `docs/architecture/` is made to live in the repo.
- **Wiki/Confluence/ticket** — attach or embed `index.html`; it renders
  standalone.
- **Local preview** — open the file directly, or `python3 -m http.server` in
  `docs/architecture/`.
- **Offline / blocked CDN** — the page tries `./mermaid.min.js` first, then the
  jsDelivr CDN. If diagrams don't render on a locked-down network, download
  `mermaid.min.js` (any recent version) and place it beside `index.html`.

## Regenerating

Re-run the agent after refactors — it stamps each file with the date + commit
SHA so staleness is visible. It always asks before overwriting; say yes to
refresh the map. If your layout changed a lot, delete `docs/architecture/` and
regenerate from scratch.

## CodeSee and other tools

[CodeSee](https://www.codesee.io) is a hosted product with its own scanning and
maps — this agent can't drive it. If your org has CodeSee, use it for live
repo maps; use this agent for committable docs and the plain-language explainer.
The mermaid sources in `ARCHITECTURE.md` can be pasted into other diagram tools
(mermaid.live, many wiki plugins).

## Troubleshooting

| Problem | Fix |
|---|---|
| Blank diagrams in index.html | CDN blocked — put `mermaid.min.js` next to `index.html` |
| Mermaid syntax error on a diagram | Regenerate with "fix diagram N"; labels should be quoted — the agent sanitizes names, but odd identifiers can slip through |
| Map is too shallow for a big monorepo | Run `/architecture-package` per module and link them, or ask for a deep dive on one module |
| "inferred" labels you disagree with | Tell it which edges to check — inferred means "probable but not proven in code" |
| Agent not in dropdown | File must be in `.github/agents/`; reload the IDE |

## Limitations

- Maps the code as it exists at the stamped commit — it doesn't run the app or
  see runtime wiring (DI containers, reflection, dynamic dispatch are marked
  inferred).
- Very large repos map at module level; per-class detail needs package scope.
- Diagrams cap at ~25 nodes — deliberate, to keep them readable.
