# agents

A collection of reusable GitHub Copilot custom agents that work in any repository.

## What are custom agents?

GitHub Copilot supports custom agents defined as Markdown files in a repo's
`.github/agents/` directory. Each `*.agent.md` file has YAML frontmatter
(`name`, `description`, optional `tools`/`model`) followed by a prompt that
defines the agent's behavior.

This repo is the central home for agents that are useful across projects —
copy them into any repo's `.github/agents/` directory to use them.

## Available agents

| Agent | Description |
|---|---|
| [code-reviewer](agents/code-reviewer.agent.md) | Reviews diffs and PRs for correctness, security, and maintainability |
| [test-writer](agents/test-writer.agent.md) | Writes tests that match the repo's existing conventions |
| [debugger](agents/debugger.agent.md) | Systematic root-cause debugging workflow |
| [refactor](agents/refactor.agent.md) | Safe, incremental refactoring without behavior changes |
| [docs-writer](agents/docs-writer.agent.md) | Writes and updates documentation from the code itself |
| [java-modernizer](agents/java-modernizer.agent.md) | Senior-architect advisor for legacy Java — scopes analysis to the current change + collaborators, produces a prioritized modernization plan, applies approved items |

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

### Option 2: manual

```bash
mkdir -p .github/agents
cp /path/to/agents/agents/*.agent.md .github/agents/
```

Commit the files so they're available to anyone (and to Copilot coding agent)
working in that repo.

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

## Writing tips

- `description` is how users/agents decide when to invoke it — make it specific.
- Reference the repo's own conventions ("follow existing test style") rather
  than hardcoding preferences.
- Prefer `tools:` restrictions for read-only agents like reviewers.
