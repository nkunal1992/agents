# Speaker notes — Raising Quality with Four Copilot Agents

Theme: **quality improvement** — what each agent improves, and how the
team uses it day to day. Agents in alphabetical order, matching the deck.
Target: ~15–18 min + demo/questions.

## Opening (30–45 sec)

- "Quality doesn't fail in one big place — it leaks in a hundred small
  ones: untestable legacy code, missing specs, vague tickets, rotting
  vulnerability suppressions."
- "These are four Copilot agents — each literally one Markdown file —
  that plug a different leak. Same contract in all four: *they propose,
  a human approves.*"
- "What I'll show for each: the quality problem → how it works → how the
  team uses it → what it measurably improves."

---

## 1. code-to-requirements 📐

- **Hook:** "The most ambitious one. Legacy system, no spec — and an
  audit or rebuild needs a functional requirements document that would
  take a human months to write."
- **Quality framing:** "A wrong spec is worse than no spec — everyone
  downstream builds on it. This agent's whole design is about making the
  spec *provable*, not just readable."
- **How:** "Backward slicing — you name the output files; it traces every
  field back through the code to its inputs. Every branch, negative path,
  sequential override, and *silent* input — configs, env vars, feature
  flags — is captured."
- **Verification:** "Every rule is dry-run against a synthetic input
  before it's written. Where code and expectation disagree, the spec
  gets corrected."
- **Human gates:** "Three approvals — the plan, the data dictionary, a
  'golden sample' output — then it runs autonomously with checkpointed,
  resumable state. Gate 3 pauses on any ambiguity."
- **Honesty line:** "Untraceable → `[STATUS: UNRESOLVED]` — never
  guessed. The `.adoc` is a pure spec; file:line evidence stays in the
  internal state file as the audit trail."
- **How the team uses it:** "Before a rebuild or migration — extract the
  spec first so the new team builds from truth. For audits — 'shall'
  language + immutable IDs are evidence-ready. And the handoff button
  sends the spec to test-writer, so the verification appendix becomes
  real tests."
- **Quality outcome:** "Verified specs → generated tests → caught
  regressions. Quality upstream is quality everywhere."

## 2. cve-remediator 🛡️

- **Hook:** "Scan fails the build; someone owns a pile of CVEs. Upgrade
  too far → regressions; too little → still vulnerable. And the
  suppression file rots quietly."
- **Quality framing:** "The real risk isn't the CVE list — it's the
  *stale* list. Entries keep hiding CVEs that became fixable months ago."
- **How:** "Give it a scan log, URL, file, or bare CVE IDs — it
  normalizes findings from every major scanner, finds where each version
  is truly declared, looks up OSV/GHSA/NVD, and picks the **smallest
  fixing version** — patch first."
- **Enterprise detail:** "It verifies the fix resolves through our
  internal mirror — otherwise 'fixed upstream, not yet in mirror.'
  Internal package names never go to public services; credentials are
  never printed."
- **Approval:** "Report → stop → you reply `all safe` (patch/minor only,
  never majors or new suppressions) → it applies, regenerates lockfiles
  properly, runs the build, re-scans, and hands you a commit message and
  PR description."
- **Killer feature:** "Every run audits every whitelist entry — already
  fixed → delete; fix available → upgrade + delete together; unfixed →
  keep but add justification and expiry. The whitelist becomes a living
  document."
- **How the team uses it:** "Every failed security scan goes straight to
  it; and quarterly, run it with no input for a whitelist-only audit."
- **Quality outcome:** "Fewer open CVEs, fewer stale suppressions, less
  regression risk per fix — and it's all evidence-cited."

## 3. java-modernizer ☕

- **Hook:** "Untestable legacy Java means defects hide until production.
  'Modernize the codebase' projects always die."
- **Quality framing:** "Not a rewrite — every change you're already
  making leaves the code a little better. Compound interest."
- **How:** "Six phases: establish your diff → detect JDK/JUnit/DI (never
  suggests APIs above your Java level) → map collaborators one hop out →
  assess gaps *testability first* → prioritized report → implement only
  what you pick."
- **Testability first:** "It finds why a class can't be tested —
  dependencies `new`ed in constructors, static calls, hidden singletons —
  and injects a seam. That unlocks the unit tests the codebase never had."
- **Safety in untested code:** "Before risky moves it proposes a
  *characterization test* — pin current behavior so a 'safe' refactor
  can't silently change it. Mechanical, provably-safe steps only."
- **How the team uses it:** "Run it on every legacy Java diff. Each
  finding explains *why* — the team's judgment improves with the code."
- **Quality outcome:** "More unit-testable classes, fewer hidden
  dependencies, less coupling per release."

## 4. jira-ticket-writer 🎫

- **Hook:** "Work is done; the ticket is an afterthought. Vague tickets
  degrade review, QA, and traceability — ticket quality varies wildly."
- **Quality framing:** "This is communication quality. Bad tickets cause
  weak reviews, wrong QA focus, and reopened issues."
- **How:** "Point it at your diff — worktree, commit, range, branch. It
  reads the hunks plus surrounding code, pulls the ticket key from the
  branch name, and writes a paste-ready ticket: fields *with stated
  basis*, summary, structured description, acceptance criteria mapped to
  the diff."
- **Honesty:** "It describes what the code *does* — inferred intent is
  labelled, missing business context gets asked, not invented. Secret
  scan runs before writing; it can never call JIRA or edit files."
- **How the team uses it:** "Every finished change → ticket draft before
  touching JIRA. QA gets 'How to test' steps derived from the actual
  diff. One word iterates it: `cloud`, `split`, field overrides."
- **Quality outcome:** "Fewer clarification round-trips, fewer reopened
  tickets, consistent quality no matter who writes it."

---

## The daily pattern (the "team utilization" slide)

"Here's how it fits our day: legacy Java diff → modernizer before commit.
Work finished → ticket draft before JIRA. Security scan fails →
cve-remediator. Undocumented module → C2R. And handoffs chain them —
ticket → code-reviewer, spec → test-writer, CVE fix → code-reviewer."

## Metrics slide — how to talk about it

- "Baseline these for our team — even directional movement tells the
  story: UNRESOLVED count, stale suppressions removed, newly testable
  classes, ticket reopen rate, time-to-remediate."
- *(If asked for real numbers — have one concrete example ready from
  your own usage.)*

## Wrap-up (30 sec)

- "Quality doesn't come from heroic cleanup projects — it comes from
  small, verified, approved improvements on work we're already doing."
- "Each agent improves an artifact the *next* step depends on — spec →
  tests → review → release."
- **Closing line:** "Not autopilot — autopilot *with* a copilot."
