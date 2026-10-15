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

## Usage

### Option 1: install script

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

### Option 2: manual

```bash
mkdir -p .github/agents .github/prompts
cp /path/to/agents/agents/*.agent.md .github/agents/
cp /path/to/agents/prompts/*.prompt.md .github/prompts/
```

Commit the files so they're available to anyone (and to Copilot coding agent)
working in that repo.

### Option 3: organization-wide

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
