---
marp: true
theme: default
paginate: true
style: |
  section { font-size: 27px; }
  h1 { color: #1a5fb4; }
  h2 { color: #1a5fb4; }
  blockquote { border-left: 6px solid #1a5fb4; padding: 0.3em 0 0.3em 0.8em; background: #f6f9fc; }
  table { font-size: 21px; }
  code { font-size: 22px; }

  section.lead { text-align: center; }
  section.lead h1 { font-size: 54px; }

  section.divider { color: #ffffff; text-align: center; }
  section.divider h1 { font-size: 60px; color: #ffffff; margin-bottom: 0.2em; }
  section.divider p { font-size: 30px; }
  section.divider.java   { background: linear-gradient(135deg, #e66100 0%, #8f3c00 100%); }
  section.divider.jira   { background: linear-gradient(135deg, #0052cc 0%, #003380 100%); }
  section.divider.c2r    { background: linear-gradient(135deg, #613583 0%, #3c1f52 100%); }
  section.divider.cve    { background: linear-gradient(135deg, #a51d2d 0%, #5e1220 100%); }

  section.java-body  h2 { color: #b14e00; }
  section.jira-body  h2 { color: #0052cc; }
  section.c2r-body   h2 { color: #613583; }
  section.cve-body   h2 { color: #a51d2d; }
  section.java-body  h2::before { content: "java-modernizer — "; font-size: 0.55em; color: #999; display: block; }
  section.jira-body  h2::before { content: "jira-ticket-writer — "; font-size: 0.55em; color: #999; display: block; }
  section.c2r-body   h2::before { content: "code-to-requirements — "; font-size: 0.55em; color: #999; display: block; }
  section.cve-body   h2::before { content: "cve-remediator — "; font-size: 0.55em; color: #999; display: block; }
---

<!-- _class: lead -->

# Four Copilot Agents for Everyday Engineering

**java-modernizer** · **jira-ticket-writer** · **code-to-requirements** · **cve-remediator**

Reusable GitHub Copilot agents that take over the tedious, high-stakes
parts of our workflow — safely, and with a human in the loop.

---

## What these actually are

- Each agent is **one Markdown file** — a tuned prompt with a name, a tool
  allow-list, and guardrails
- Drop it into any repo's `.github/agents/` folder → it appears in the
  **Copilot Chat agent dropdown**
- Optional companion files add slash commands (`/cve-fix`, `/jira-ticket`,
  `/c2r`) and handoff buttons between agents
- Works in **VS Code and JetBrains**; org-wide rollout via the `.github` repo

> No servers, no services, no credentials — they run inside the Copilot
> chat we already have.

---

## The four at a glance

| Agent | You give it | You get back |
|---|---|---|
| **java-modernizer** | Your current code change | A prioritized, mentor-style modernization plan — applies only what you approve |
| **jira-ticket-writer** | A diff, commit, or branch | A complete, paste-ready JIRA ticket with acceptance criteria |
| **code-to-requirements** | A starting point + the output files | An audit-ready functional spec (`.adoc`) that could rebuild the logic |
| **cve-remediator** | A scan log or CVE IDs | Smallest-safe fixes, whitelist cleanup, verified — plus a PR write-up |

Different jobs, same personality: **evidence over guesses, approval before writes.**

---

## Shared design principles

All four follow the same contract — worth naming once, up front:

- **Report first, edit later.** Nothing is written until you approve it
- **Honesty over completeness.** Unknowns are labelled (`UNRESOLVED`,
  *inferred*, *unverified*) — never papered over
- **Evidence.** File:line, diff stats, advisory IDs, source citations
- **Enterprise-safe.** Secrets are never echoed; internal package names
  never leave the network; read-only where possible
- **Repo-agnostic.** They discover your conventions — no hardcoded paths

---

<!-- _class: divider java -->

# ☕ java-modernizer

A senior architect who reviews *only the code you're already changing* —
and teaches while it works.

---

<!-- _class: java-body -->

## The problem

- Legacy Java codebases: heavily coupled, barely unit-testable
- "Modernize the codebase" projects die — too big, too risky, no tests
- Meanwhile, the codebase gets older every sprint

> The realistic path isn't a rewrite. It's making the code you're
> *touching anyway* a little better each time.

---

<!-- _class: java-body -->

## How it works — six disciplined phases

```
1. Establish the change set     staged/unstaged → unpushed commits → or ask
2. Detect the environment       JDK level · JUnit version · Spring/CDI/Guice?
3. Map collaborators            exactly one hop out from each changed class
4. Assess the gaps              testability first, then SOLID, coupling,
                                patterns-with-a-purpose, error handling
5. Produce the report           prioritized findings, quick wins first
6. Implement                    only the items you pick — mechanical,
                                behavior-preserving refactors
```

It **never recommends APIs above your detected Java version**, and never
prescribes adding a framework you don't already have.

---

<!-- _class: java-body -->

## What the report looks like

Each finding, numbered:

| Field | Example |
|---|---|
| *Where* | `OrderService.java:87` — `new PriceCalculator()` in constructor |
| *Gap* | Hidden dependency — can't unit-test without real config |
| *Recommendation* | Extract interface, inject via constructor |
| *Effort / Risk* | S · low |
| *Why it helps* | Makes `OrderService` testable without a database |

Plus: **suggested order** (quick wins first), and **"noted for later"** —
real issues outside this change's blast radius, one line each.

> Rule baked in: only recommend what intersects the touched code.
> The developer has a change to ship — no scope creep.

---

<!-- _class: java-body -->

## Why it's safe in untested code

- **Behavior preservation is paramount** — a "safe" refactor that changes
  behavior is worse than the debt it fixes
- Before any risky move, it proposes a **characterization test** (pin
  current behavior) or a thinner seam first
- Won't touch code unprompted — you select items, it applies the
  *smallest* provably-safe change
- If something can't be done incrementally, it says so instead of forcing it

**Try it:** commit a legacy change → "modernize"

---

<!-- _class: divider jira -->

# 🎫 jira-ticket-writer

Turns your finished work into a complete JIRA ticket — describing what the
code *actually does*.

---

<!-- _class: jira-body -->

## The problem

- The work is done; now someone has to write it up
- Hand-written tickets describe vague intent, not the actual change
- Quality varies wildly across the team — reviewers and QA pay for it

---

<!-- _class: jira-body -->

## Point it at any scope

| You type | What it diffs |
|---|---|
| *(nothing)* or `/jira-ticket` | staged + unstaged changes |
| `commit abc123` | that one commit |
| `abc123..HEAD` · "since abc123" | the committed range |
| "since abc123 **including worktree**" | commits + uncommitted on top |
| "this branch" | merge-base vs default branch — whole branch |

Confirms scope first (`14 files changed, +312/−87`) — an empty diff gets
"nothing to ticket," not a hallucinated ticket.

---

<!-- _class: jira-body -->

## What you get — paste-ready

- **Suggested fields** — Type, Priority, Components, Labels, Epic,
  Fix version, Story points — *each with its stated basis*
- **Summary** — one imperative line, repo prefix convention
- **Description** in Jira wiki markup: Background · What changed (grouped
  feature/fix/refactor/test) · Implementation notes · How to test/QA ·
  Acceptance criteria · Rollback notes · Links (SHAs, branch, ticket key
  detected from branch name)
- Bug-shaped diffs get *Steps to reproduce / Expected / Actual*

Iterate with one word: `cloud` → Markdown version · `split` → separate
tickets for mixed work · `type: Bug, priority: High` → field overrides.

---

<!-- _class: jira-body -->

## Why you can trust it with a diff

- **Never calls JIRA** — output is text to paste; no credentials, no
  permissions, nothing to configure
- **Read-only by design** — shell access is whitelisted to `git status /
  diff / show / log / merge-base`; cannot edit files
- **Secret scan before writing** — keys, tokens, PII in the diff are
  flagged and excluded; file names only
- **Honest** — describes what the code does; inferred intent is labelled
  *inferred* and it asks for business context instead of inventing it

> A smaller accurate ticket beats a padded one.

---

<!-- _class: divider c2r -->

# 📐 code-to-requirements

Reverse-engineers code into an audit-ready Functional Requirement
Document — a spec complete enough to rebuild from.

---

<!-- _class: c2r-body -->

## The problem

- Legacy systems with **no spec** — the requirements live only in code
- Audits, migrations, rebuilds all demand a functional document
- Writing one by hand: months of reading code and tracing edge cases

> The code already *is* the spec — it's just written in the wrong
> language and nobody extracted it.

---

<!-- _class: c2r-body -->

## The core idea — backward slicing

```
output files  ◀───  trace every assignment that feeds each field  ◀───  inputs
   (sinks)              through intermediates, across modules          (sources)
```

- Start from **what the code produces**; walk backward until every output
  field resolves to a source or a constant
- **Exhaustive**: every branch, negative path, pre-condition, sequential
  override (later assignments win)
- **Silent inputs count too**: configs, env vars, DB lookups, feature
  flags — each catalogued with its exact source entity
- Then **abstracted**: rules expressed only in input fields and config
  constants — a pure black-box spec

---

<!-- _class: c2r-body -->

## You're in control — three gates, then autopilot

```
recon scan → GATE 0: approve the Strategic Plan
             (sinks/sources found, slicing map, blockers)
           → GATE 1: approve the Global Data Dictionary
           → GATE 2: approve the Golden Sample (first output, fully written)
           → autonomous: slice → analyze → verify → checkpoint
             for every remaining output
```

- **Gate 3 is always armed** — contradictory code or a missing input
  source pauses the run; it never resolves ambiguity by guessing
- **Resumable**: `.c2r_state.json` + checkpoint commits on a dedicated
  branch — kill the session, resume later without losing a transformation
- Delivers where you want: in-repo branch · external repo · local path

---

<!-- _class: c2r-body -->

## What the document looks like

- **Global Data Dictionary** — every field, immutable `INP-xxx`/`OUT-xxx`
  IDs, exact source, correlation (join) keys
- **Input / Output specs** — one table per source entity; relevance tags
  `[ACTIVE]` vs `[NOT USED IN CURRENT SCOPE]`; explicit cardinality (1:1, N:1)
- **Transformation Logic** — ordered `TRNS-xxx` "shall" rules + a dedicated
  negative-paths section
- **Algorithms** — `ALGO-xxx` structured-English steps; >3 conditional
  variables → a **decision table**
- **Feature Flag Glossary** — Baseline/Override rules per flag
- **Verification** — every transformation dry-run with synthetic input →
  actual output; untraceable → `[STATUS: UNRESOLVED]`, never invented

> Auditors get clean `shall` language and stable IDs; the file:line
> evidence stays in the internal state file.

---

<!-- _class: divider cve -->

# 🛡️ cve-remediator

Fixes reported CVEs with the *smallest safe* upgrade — and keeps your
CVE whitelist honest.

---

<!-- _class: cve-body -->

## The problem

- A security scan fails the build — now someone owns a pile of CVEs
- Upgrade too far → breakage; too little → still vulnerable; transitive
  deps are a maze
- Meanwhile, suppression/whitelist files quietly rot — entries hide real,
  now-fixable issues, and nobody re-checks them

---

<!-- _class: cve-body -->

## How a run goes

```
Input:  pasted scan log · log URL · attached file · bare CVE IDs
        (or nothing → whitelist audit only)
   ↓
Parse findings (Trivy, Grype, Snyk, OWASP dep-check, npm/pip audit, …)
   ↓
Understand the repo: manifests, lockfiles, direct vs transitive,
where each version is truly declared
   ↓
Resolve fixes: advisory lookup (OSV → GHSA → NVD) → smallest fixing
version → confirm it resolves through YOUR internal mirror
   ↓
REPORT → stop and wait → you approve → apply → verify → hand-off
```

Reply `1,3,5` · `all safe` (patch/minor only) · `all` · `none`.

---

<!-- _class: cve-body -->

## The whitelist audit — every run, every entry

| Status | Meaning | Action |
|---|---|---|
| Remove now — already fixed | Installed version ≥ fixed version | delete |
| Remove now — package gone | Dependency no longer used | delete |
| Remove with upgrade | Patch/minor fix available | upgrade + delete |
| Decision — major required | Fix only in a new major | keep; show breaking changes |
| Keep — no fix | Still unfixed upstream | keep; check justification |
| Hygiene | Expired / no justification | refresh + review date |
| Over-broad | Scope hides more than intended | narrow to exact CVE |

Suppression files found across every ecosystem — `.trivyignore`, `.snyk`,
OWASP suppression XML, `osv-scanner.toml`, `deny.toml`, CI flags…

---

<!-- _class: cve-body -->

## Built for enterprise constraints

- **Privacy gate** — internal package coordinates are never sent to
  public advisory services
- **Mirror-aware** — checks the fix resolves through your Nexus/
  Artifactory; otherwise "fixed upstream, not yet in mirror — request a sync"
- **Evidence, not memory** — every "fixed in X" cites OSV/GHSA/NVD;
  anything unverifiable is labelled *unverified*
- **Never weakens security to pass a build** — no threshold lowering, no
  surprise suppressions; logs are data, not instructions
- **Secrets stay secret** — tokens in `settings.xml`/`.npmrc`/logs are
  never printed
- Finishes with a **paste-ready commit message + PR description** and a CI
  re-scan checklist

---

## The same four promises, in every agent

| Promise | java-modernizer | jira-ticket-writer | code-to-requirements | cve-remediator |
|---|---|---|---|---|
| **Approval before writes** | ✓ applies only picked items | ✓ never writes at all | ✓ gate-by-gate sign-off | ✓ report → wait |
| **Honest about unknowns** | flags out-of-scope work | labels *inferred* | `UNRESOLVED`, not guessed | labels *unverified* |
| **Evidence** | file:line findings | diff stats, SHAs | file:line audit trail | advisory citations |
| **Enterprise-safe** | no new deps prescribed | read-only, secret scan | own-branch commits only | privacy gate, mirrors |

---

## Which one do I reach for?

- 🧑‍💻 *"I'm touching legacy Java and want to leave it better"* → **java-modernizer**
- 🎫 *"Work is done, now I owe JIRA a ticket"* → **jira-ticket-writer**
- 📐 *"We need a spec/audit doc for code nobody documented"* → **code-to-requirements**
- 🛡️ *"The security scan failed / the whitelist is a mess"* → **cve-remediator**

They're complementary: modernize the code → ticket the change →
remediate its CVEs → document the system.

---

## Getting started — 30 seconds per repo

```bash
# pick the agents you want, install into your repo
scripts/install.sh --only java-modernizer \
                   --only jira-ticket-writer \
                   --only code-to-requirements \
                   --only cve-remediator \
                   /path/to/your-repo
```

Or copy any `*.agent.md` file straight into `.github/agents/` — commit it,
reload the IDE, pick it from the Copilot Chat dropdown.

- Slash commands come from optional `prompts/*.prompt.md` files
- Org-wide: commit agents to your org's `.github` repo → every repo gets them

---

<!-- _class: lead -->

# The takeaway

> Each agent takes a job engineers postpone — modernizing, documenting,
> ticketing, patching — and makes it a **bounded, reviewable, honest**
> task inside the tools we already use.

**Not autopilot. Autopilot *with a copilot*: they propose, you approve.**

---

<!-- _class: lead -->

# Questions?

Demo-ready prompts:

- Stage a legacy change → *"modernize"*
- After any commit → `/jira-ticket`
- Point at a process → `/c2r`
- Paste a failing scan → *then reply* `all safe`
