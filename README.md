# agents

A collection of reusable GitHub Copilot custom agents that work in any repository.

## What are custom agents?

GitHub Copilot supports custom agents defined as Markdown files in a repo's
`.github/agents/` directory. Each `*.agent.md` file has YAML frontmatter
(`name`, `description`, optional `tools`/`model`) followed by a prompt that
defines the agent's behavior.

This repo is the central home for agents that are useful across projects —
copy them into any repo's `.github/agents/` directory to use them.

Some agents also ship **prompt files** (`prompts/*.prompt.md`), which become
slash commands such as `/cve-fix` once copied to `.github/prompts/`.

## Available agents

| Agent | Description |
|---|---|
| [code-reviewer](agents/code-reviewer.agent.md) | Reviews diffs and PRs for correctness, security, and maintainability |
| [test-writer](agents/test-writer.agent.md) | Writes tests that match the repo's existing conventions |
| [debugger](agents/debugger.agent.md) | Systematic root-cause debugging workflow |
| [refactor](agents/refactor.agent.md) | Safe, incremental refactoring without behavior changes |
| [docs-writer](agents/docs-writer.agent.md) | Writes and updates documentation from the code itself |
| [java-modernizer](agents/java-modernizer.agent.md) | Senior-architect advisor for legacy Java — scopes analysis to the current change + collaborators, produces a prioritized modernization plan, applies approved items |
| [cve-remediator](agents/cve-remediator.agent.md) | Fixes CVEs with the smallest safe upgrade and audits CVE whitelists for entries that can be removed — [guide](docs/cve-remediator.md) |
| [codebase-guide](agents/codebase-guide.agent.md) | Read-only codebase explainer — how/where/why answers and repo tours with file:line evidence, tuned for large repos — [guide](docs/codebase-guide.md) |
| [jira-ticket-writer](agents/jira-ticket-writer.agent.md) | Generates paste-ready JIRA ticket details from worktree diffs, commits, or ranges — [guide](docs/jira-ticket-writer.md) |
| [architecture-mapper](agents/architecture-mapper.agent.md) | Maps repo/package architecture into mermaid diagrams + a self-contained explainer site — [guide](docs/architecture-mapper.md) |
| [code-to-requirements](agents/code-to-requirements.agent.md) | Reverse-engineers code into an audit-ready .adoc functional spec — slicing, exhaustive paths, verification loop — [guide](docs/code-to-requirements.md) |
| [confluence-knowledge](agents/confluence-knowledge.agent.md) | Single-file grounded Q&A over one Confluence page + its descendant pages — self-installs its local MCP service on first run (bundled zip available via `scripts/bundle-confluence.sh`), per-claim citations, explicit "not documented" refusals — [guide](docs/confluence-knowledge.md) |

## What each agent does — and why it matters at work

Each entry: **what it does** → **why it helps in an enterprise** → **try it**.
All agents are repo-agnostic and never expose secrets; most pin cheap models
first (`GPT-6 Luna` → `GPT-5 mini` → `GPT-4.1` fallbacks) — confluence agents
instead inherit your company's approved Copilot model. Read-only agents can't
modify your code by design. `confluence-knowledge` is the only agent that
needs a local companion service.

### code-reviewer
Reviews diffs, staged changes, and PRs for correctness, security, and
maintainability. *Why it matters:* a consistent second pair of eyes on every
change — catches defect classes humans skim over, without touching code
(read-only). *Try:* stage a change → "review this".

### test-writer
Writes tests that match the repo's existing frameworks, naming, and fixtures.
*Why:* raises coverage without style drift — new tests look like your senior
devs wrote them, so review friction stays low. *Try:* "write tests for
`OrderService`".

### debugger
Systematic root-cause workflow: reproduce → trace → isolate → fix → verify —
no shotgun changes. *Why:* turns debugging from guesswork into a repeatable,
evidence-based process; an audit trail of what was ruled out. *Try:* paste a
stack trace or failing test.

### refactor
Safe, incremental refactoring — smallest behavior-preserving steps, verified
between each. *Why:* modernization without the big-bang risk; every step is
revertible and reviewable. *Try:* "extract this method's discount logic".

### docs-writer
Writes and updates documentation derived from the actual code — READMEs,
guides, API docs. *Why:* docs stop drifting from code; generated from source
truth, so nothing is invented. *Try:* "document the authentication flow".

### java-modernizer
Senior-architect advisor for legacy Java: scopes analysis to your staged
changes + their direct collaborators, detects the Java version/framework, and
produces a prioritized modernization plan (testability seams first) — then
implements only the items you approve. *Why:* modernization advice that
respects your stack's constraints and never suggests APIs your JDK can't run.
*Try:* commit some legacy changes → "modernize"

### cve-remediator
Fixes CVEs with the *smallest safe* upgrade — patch before minor before major —
checks fixed versions exist in your Nexus/Artifactory mirror, audits your CVE
whitelist on every run, and recommends removing suppressions when real fixes
exist. Internal package names never leave your network. *Why:* remediation
that's auditable end-to-end — every version choice is evidence-backed, every
whitelist entry gets a disposition (fixable / already-fixed / keep / needs
review). *Try:* `/cve-fix` or paste a scanner log · [guide](docs/cve-remediator.md)

### codebase-guide
Read-only codebase explainer: "how does X work?", guided repo tours, where-is
questions — every claim anchored `file:line`, with a confidence section
separating proven from inferred. *Why:* kills tribal-knowledge onboarding —
new engineers get evidence-backed answers in minutes instead of days of
code-spelunking. Physically cannot modify the repo. *Try:* `/codebase-tour` ·
[guide](docs/codebase-guide.md)

### jira-ticket-writer
Generates complete, paste-ready JIRA ticket details from your actual changes —
staged/unstaged diffs, a commit SHA, a range, or a branch — including summary,
description (Jira wiki-markup or Markdown), acceptance criteria, test notes,
and rollback. *Why:* tickets describe what the code *actually* does rather
than vague intent — consistent quality for the whole team; and it writes
nothing back to JIRA, so no credentials or permissions needed. *Try:*
`/jira-ticket` after staging work · [guide](docs/jira-ticket-writer.md)

### architecture-mapper
Maps repo or package architecture into `docs/architecture/`: a Mermaid-diagram
`ARCHITECTURE.md` plus a self-contained explainer `index.html` written in plain
language. *Why:* instant, evidence-backed architecture docs for reviews,
audits, and onboarding — the explainer site reads well even for
non-engineers/stakeholders. *Try:* `/architecture` ·
[guide](docs/architecture-mapper.md)

### code-to-requirements (C2R)
Reverse-engineers code into an audit-ready AsciiDoc functional spec: backward
slices from output sinks to input sources, catalogs every field per source
(with active/unused relevance), maps all branches and negative paths, captures
feature flags as Baseline/Override rules, decision-tables complex logic, and
verifies every transformation with a synthetic-input dry run. Human-in-the-loop
gates (Plan → Foundation → Golden Sample) keep you in control; `.c2r_state.json`
makes long runs resumable; deliverables can go in-repo, to an external repo, or
a local path. Untraceable paths are marked `UNRESOLVED`, never guessed.
*Why:* produces the kind of pure functional specification auditors and
rebuild-teams actually need — with the technical evidence retained internally
for traceability. *Try:* `/c2r` · [guide](docs/code-to-requirements.md)

### confluence-knowledge
A single agent file: drop it into `.github/agents/` (or your agent store) and
on first run it installs its own local companion service — you approve each
step, then reload once. After that it answers questions strictly from a
Confluence page subtree you choose: root page + all descendants, crawled and
indexed locally (`.conf_knowledge_index/`), semantic + keyword retrieval
reranked by a local model, and every claim carries a clickable source
citation. The service is hard-coded GET-only — it physically cannot write to
Confluence — and its embeddings run entirely on the laptop (Apache-2.0
models, no cloud calls). Missing evidence gets an explicit "not documented"
answer, never a guess.
*Why:* a safe, auditable way to let teams query internal documentation
without exposing it to uncontrolled tooling — read-only by construction in
the service, credentials in the OS keyring, subtree access rechecked on every
refresh. *Try:* `/confluence-ask` · [guide](docs/confluence-knowledge.md)
*Tradeoff to know:* the file carries `execute`/`edit` for first-run install —
bootstrap commands are whitelisted, and Q&A mode never uses them.

## Usage

### Option 1: copy a single file (no clone needed)

Every `*.agent.md` file is self-contained and carries an install header inside
it. On GitHub, open the agent you want → **Raw** → copy/save it into your
repo's `.github/agents/` directory — that's it. Open Copilot Chat and pick the
agent from the dropdown.

Companion `*.prompt.md` files (slash commands) are optional — their headers
name the agent they need; copy them into `.github/prompts/`. Handoff buttons
just won't appear if a target agent isn't installed — the agent still works.

### Option 2: install script

From the repo you want the agents in:

```bash
/path/to/agents/scripts/install.sh
```

Or copy to a specific repo:

```bash
/path/to/agents/scripts/install.sh /path/to/target-repo
```

Install only specific agents (plus their prompt files) with `--only`:

```bash
/path/to/agents/scripts/install.sh --only cve-remediator --only code-reviewer /path/to/target-repo
```

Existing files are never overwritten. Run `install.sh --help` for all options.

### Option 3: manual (clone)

```bash
mkdir -p .github/agents .github/prompts
cp /path/to/agents/agents/*.agent.md .github/agents/
cp /path/to/agents/prompts/*.prompt.md .github/prompts/
```

Commit the files so they're available to anyone (and to Copilot coding agent)
working in that repo.

### Option 4: organization-wide

Commit agent files to the `agents/` folder of your organization's `.github` or
`.github-private` repository to make them available in every repository of the
organization. Prompt files are not distributed this way; install them per repo.

## Adding a new agent

1. Create `agents/<name>.agent.md` with frontmatter:

   ```markdown
   ---
   name: my-agent
   description: What it does and when to use it
   ---

   You are... <instructions>
   ```

2. Keep instructions **repo-agnostic** — no project-specific paths, tools, or
   services. Agents should discover conventions from the repo they're running in.
3. Add a row to the table above.
4. Optional: add slash commands as `prompts/<name>.prompt.md` with
   `agent: <agent-name>` in the frontmatter — `install.sh --only <agent-name>`
   picks them up automatically.

## Writing tips

- `description` is how users/agents decide when to invoke it — make it specific.
- Reference the repo's own conventions ("follow existing test style") rather
  than hardcoding preferences.
- Prefer `tools:` restrictions for read-only agents like reviewers.
