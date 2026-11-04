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
  section.divider.c2r    { background: linear-gradient(135deg, #613583 0%, #3c1f52 100%); }
  section.divider.cve    { background: linear-gradient(135deg, #a51d2d 0%, #5e1220 100%); }
  section.divider.java   { background: linear-gradient(135deg, #e66100 0%, #8f3c00 100%); }
  section.divider.jira   { background: linear-gradient(135deg, #0052cc 0%, #003380 100%); }

  section.c2r-body   h2 { color: #613583; }
  section.cve-body   h2 { color: #a51d2d; }
  section.java-body  h2 { color: #b14e00; }
  section.jira-body  h2 { color: #0052cc; }
  section.c2r-body   h2::before { content: "code-to-requirements — "; font-size: 0.55em; color: #999; display: block; }
  section.cve-body   h2::before { content: "cve-remediator — "; font-size: 0.55em; color: #999; display: block; }
  section.java-body  h2::before { content: "java-modernizer — "; font-size: 0.55em; color: #999; display: block; }
  section.jira-body  h2::before { content: "jira-ticket-writer — "; font-size: 0.55em; color: #999; display: block; }

---

<!-- _class: lead -->

# Raising Quality with Four Copilot Agents

**code-to-requirements** · **cve-remediator** · **java-modernizer** · **jira-ticket-writer**

How our team uses reusable Copilot agents to improve the quality of our
code, our specs, our tickets, and our security posture.

---

## Where quality leaks in daily work

- **Legacy code** — untestable classes mean defects hide until production
- **Specs** — undocumented logic makes audits and rebuilds guesswork
- **Tickets** — vague write-ups produce weak reviews and weak QA
- **Vulnerabilities** — risky upgrades get postponed; suppressions rot

> Quality isn't one big fix — it's a hundred small leaks.
> Each agent plugs a different one.

---

## What these actually are

- Each agent is **one Markdown file** — a tuned prompt with a tool
  allow-list and guardrails
- Drop into `.github/agents/` → appears in the **Copilot Chat dropdown**
- Optional slash commands (`/c2r`, `/cve-fix`, `/jira-ticket`) and
  handoff buttons between agents
- **VS Code + JetBrains**; org-wide rollout via the `.github` repo

> Same contract in all four: **they propose, a human approves.**

---

## The four — and the quality lever each one pulls

| Agent | Quality lever |
|---|---|
| **code-to-requirements** | Spec accuracy — docs that can be verified, not just written |
| **cve-remediator** | Security posture — real fixes, un-hidden risks, less regression |
| **java-modernizer** | Code quality — testability and maintainability on every change |
| **jira-ticket-writer** | Communication quality — tickets that describe the actual code |

---

<!-- _class: divider c2r -->

# 📐 code-to-requirements

*Quality lever:* specs an auditor can trust — because every rule was
proven against the code.

---

<!-- _class: c2r-body -->

## The quality problem

- Legacy systems ship **no spec** — requirements live only in code
- Hand-written specs drift from reality and can't be verified
- Audits, migrations, rebuilds inherit every guess the writer made

> A wrong spec is worse than no spec — everyone downstream builds on it.

---

<!-- _class: c2r-body -->

## How it works — backward slicing

```
output files  ◀───  every assignment feeding each field  ◀───  inputs
   (sinks)         through intermediates, across modules      (sources)
```

- Exhaustive: every branch, negative path, pre-condition, sequential
  override, and **silent input** (configs, env vars, DB lookups, flags)
- Then abstracted into input-only **"shall"** rules — a pure black-box spec

**Verification loop:** each transformation is dry-run against synthetic
input → output; the spec is corrected where they disagree.

---

<!-- _class: c2r-body -->

## Why the output is *trustworthy*

- **Human gates** — approve the plan, the data dictionary, and a "golden
  sample" output before it runs autonomously; Gate 3 stops on ambiguity
- **Immutable IDs** (`INP-001`/`TRNS-003`/`FLAG-001`) — never renumbered,
  so audits trace rules forever
- **Evidence separation** — the `.adoc` is pure spec; file:line proof
  stays in the internal state file
- **Resumable** — checkpointed state; a 12-output run survives restarts

> ✦ **The quality bar:** untraceable = `[STATUS: UNRESOLVED]`,
> never guessed.

---

<!-- _class: c2r-body -->

## How the team uses it

- **Before a rebuild/migration** — extract the spec first; the new team
  builds from truth, not archaeology
- **For audits** — formal "shall" language + stable IDs = evidence-ready
- **Spec → tests** — one-click handoff to the test-writer agent; the
  verification appendix becomes real tests
- **Re-verify** — `/c2r-verify` re-checks an existing spec after code
  changes; discrepancies get listed and fixed

> Quality compounds: a verified spec → better tests → fewer regressions.

---

<!-- _class: divider cve -->

# 🛡️ cve-remediator

*Quality lever:* fixes that are small, proven, and honest — plus a
whitelist that stops hiding real risk.

---

<!-- _class: cve-body -->

## The quality problem

- Scan fails the build → someone owns a pile of CVEs
- Upgrade too far → regressions; too little → still vulnerable
- Suppression files silently rot — entries hide CVEs that became fixable
  months ago

> The real quality risk isn't the CVE list — it's the *stale* list.

---

<!-- _class: cve-body -->

## How it works — evidence over memory

```
scan log / URL / file / CVE IDs  →  normalize every finding
→ find each version's true source (POM property, catalog, lockfile)
→ advisory lookup: OSV → GHSA → NVD
→ smallest fixing version: patch → minor → major
→ confirm it resolves through OUR mirror (Nexus/Artifactory)
→ REPORT → you approve → apply → verify → PR write-up
```

- Transitives fixed via the **lowest direct parent**, not blind overrides
- `all safe` = patch/minor only — never majors, never new suppressions

---

<!-- _class: cve-body -->

## The whitelist audit — quality nobody was checking

Every run, every suppression, every ecosystem (`.trivyignore`, `.snyk`,
OWASP XML, CI flags…) gets a verdict:

| Verdict | Team action |
|---|---|
| Remove now — already fixed / package gone | delete |
| Remove with upgrade | upgrade + delete together |
| Decision — major required | keep; breaking changes shown |
| Keep — no fix | keep; justification + expiry added |
| Over-broad / hygiene | narrowed, dated, justified |

> ✦ The whitelist becomes a living document — nothing hides
> what a fix already solved.

---

<!-- _class: cve-body -->

## How the team uses it

- **Every failed security scan** → paste the log → `all safe`
- **Quarterly hygiene** → run with no input = whitelist-only audit
- **Enterprise-safe by design** — internal package names never leave the
  network; credentials never printed; unverifiable items labelled
  *unverified*
- **Hand-off included** — paste-ready commit message + PR description +
  a CI re-scan checklist

> ✦ Measurable: fewer open CVEs, fewer stale suppressions,
> less regression risk per fix.

---

<!-- _class: divider java -->

# ☕ java-modernizer

*Quality lever:* testability and maintainability — every touched change
leaves the code better.

---

<!-- _class: java-body -->

## The quality problem

- Legacy Java: heavily coupled, barely unit-testable → defects hide
- "Modernize the codebase" projects die — too big, too risky
- Codebases age every sprint while the big fix never comes

> The realistic path: make the code you're *touching anyway* better
> each time — compound interest, not a rewrite.

---

<!-- _class: java-body -->

## How it works — six disciplined phases

```
1. Change set        staged/unstaged → unpushed commits → or ask
2. Environment       JDK level · JUnit · DI framework — nothing invented
3. Collaborators     exactly one hop out from each changed class
4. Gap assessment    TESTABILITY FIRST → SOLID → coupling →
                     patterns-with-a-purpose → error handling
5. Report            effort + risk + rationale per finding, quick wins first
6. Implement         only items you pick — mechanical, provably-safe
```

Never suggests APIs above your JDK, never prescribes a framework you
don't have, never expands scope past the touched code.

---

<!-- _class: java-body -->

## How the team uses it — quality on every commit

- **Testability first** — it finds why a class can't be tested (deps
  `new`ed in constructors, static calls) and injects a seam — unlocking
  the unit tests the codebase never had
- **Characterization tests** — before risky moves, it pins current
  behavior so a "safe" refactor can't silently change it
- **Mentor effect** — every finding explains *why*; the team's judgment
  improves alongside the code
- **Report → approve → apply** — smallest behavior-preserving change,
  then it runs the build

> ✦ Measurable: more unit-testable classes, fewer hidden
> dependencies, less coupling per release.

---

<!-- _class: divider jira -->

# 🎫 jira-ticket-writer

*Quality lever:* tickets that describe what the code actually does —
better reviews, better QA, honest scope.

---

<!-- _class: jira-body -->

## The quality problem

- Work is done; the ticket is an afterthought
- Vague intent-based tickets → weak reviews, wrong QA focus, reopened
  issues
- Ticket quality varies wildly across the team

> Bad tickets don't just annoy — they degrade every downstream step:
> review, testing, and audit traceability.

---

<!-- _class: jira-body -->

## What you get — paste-ready, evidence-based

Point it at a diff — worktree, commit, range, or whole branch:

- **Suggested fields** — Type, Priority, Components, Epic, Story points,
  each *with its stated basis*
- **Description** in Jira wiki markup — Background · What changed
  (grouped) · Implementation notes · **How to test / QA** · Acceptance
  criteria · Rollback notes · Links (SHAs, branch, detected ticket key)
- Iterate in one word: `cloud` (Markdown) · `split` (separate tickets) ·
  field overrides

Bug-shaped diffs get *Steps to reproduce / Expected / Actual*.

---

<!-- _class: jira-body -->

## How the team uses it — quality of communication

- **Every finished change** → ticket draft before touching JIRA —
  consistent quality regardless of who writes it
- **Describes what the code does** — inferred intent is labelled
  *inferred*; missing business context gets asked, not invented
- **QA gets real test steps** — derived from the actual diff, not memory
- **Secret scan before writing** — credentials/PII in the diff are
  flagged and excluded
- **Read-only** — never calls JIRA, never edits files; output is text

> ✦ Measurable: fewer clarification round-trips, fewer
> reopened tickets, traceable commit-to-ticket links.

---

## How the team uses them — the daily pattern

```
Write/change code
   ├─ legacy Java diff → java-modernizer   (leave it better + testable)
   ├─ work finished    → jira-ticket-writer (accurate ticket for JIRA)
   ├─ scan fails       → cve-remediator     (smallest verified fix)
   └─ undocumented code→ code-to-requirements (audit-ready spec)
```

Handoffs chain them: ticket → **code-reviewer** · spec → **test-writer** ·
CVE changes → **code-reviewer**. One repo file each — every repo, same
workflow.

---

## The compounding quality loop

- Verified **specs** → generated tests → caught regressions
- Accurate **tickets** → sharper reviews → better QA coverage
- Testable **code** → real unit tests → defects found pre-merge
- Honest **CVE posture** → real exposure visible → fixes actually land

> Each agent improves an artifact the *next* step depends on —
> quality upstream is quality everywhere.

---

## The guardrails ARE the quality story

| Promise | How it's enforced |
|---|---|
| Human approves before writes | Gates (C2R) · report-then-wait (CVE) · pick-items (modernizer) · never-writes (tickets) |
| Honest about unknowns | `UNRESOLVED` · *inferred* · *unverified* — labels, not guesses |
| Evidence | file:line · diff stats · advisory IDs · citation trails |
| Enterprise-safe | secret scans · privacy gate · mirror checks · read-only where possible |

> Trust is what makes these usable at all — the guardrails are the feature.

---

## What we can measure

| Lever | Candidate metric |
|---|---|
| Specs | Transformation coverage · UNRESOLVED count trending down |
| Security | Open CVEs · stale whitelist entries removed · time-to-remediate |
| Code | Newly unit-testable classes · findings applied per sprint |
| Tickets | Reopened/clarification rate · review cycle time |

*(Baseline these for your team — even directional movement tells the story.)*

---

<!-- _class: lead -->

# The takeaway

> Quality doesn't come from heroic cleanup projects.
> It comes from **small, verified, approved improvements** on the work
> we're already doing — in code, specs, tickets, and security.

**Not autopilot. Autopilot *with* a copilot: they propose, you approve.**

---

<!-- _class: lead -->

# Questions?

Demo-ready prompts:

- Point at a process → `/c2r` · paste a failing scan → `all safe`
- Stage a legacy change → *"modernize"* · after any commit → `/jira-ticket`
