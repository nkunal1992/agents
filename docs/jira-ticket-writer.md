# jira-ticket-writer

Turn finished work into a complete JIRA ticket. Point it at a diff — your
working tree, a commit, or everything since a base commit — and get paste-ready
ticket details: summary, structured description, suggested fields, acceptance
criteria.

- **Honest**: describes what the code *does*, not what was intended — inferred
  intent is labeled, and it asks for business context instead of inventing it.
- **Enterprise-safe**: scans the diff for credentials/secrets before writing,
  and never echoes them into the ticket. Never calls JIRA — output is text.
- **Read-only**: cannot edit files; shell access is whitelisted to read-only git.

## Quick start

```bash
scripts/install.sh --only jira-ticket-writer /path/to/your-repo
# optional, powers the handoff buttons:
scripts/install.sh --only jira-ticket-writer --only code-reviewer --only codebase-guide /path/to/your-repo
```

Pick **jira-ticket-writer** in the Copilot Chat agent dropdown and describe the
scope — or just hit enter for your current worktree.

## Scopes

| You type / run | What it diffs |
|---|---|
| *(empty)* or `/jira-ticket` | staged + unstaged changes |
| `commit abc123` or `/jira-ticket-commit abc123` | that one commit |
| `abc123..HEAD` or "since abc123" | the committed range |
| "since abc123 including worktree" or `/jira-ticket-since abc123` | commits **and** staged/unstaged on top of the base |
| "this branch" | merge-base vs the default branch — whole branch |

It confirms scope first (`N files changed, +X/−Y`) before drafting. An empty
diff gets a clear "nothing to ticket" instead of a hallucinated ticket.

## What you get

```
## JIRA Ticket — draft
### Suggested fields   Type · Priority · Components · Labels · Epic/Parent
                       · Fix version · Story points — each with its basis
### Summary            one line, imperative, ≤80 chars
### Description        Jira wiki markup in a fenced block — paste-ready
```

Description sections: **Background** · **What changed** (grouped by
feature/fix/refactor/test/config) · **Implementation notes** · **How to test /
QA** · **Acceptance criteria** · **Rollback / deployment notes** · **Links**
(commit SHAs, branch, detected ticket key like `ABC-123` from the branch name).

Bug-shaped changes get **Steps to reproduce / Expected / Actual** where
inferable.

### Follow-up replies

| Reply | Result |
|---|---|
| `cloud` | Re-emits the description as Markdown for the Jira Cloud editor |
| `split` | Breaks unrelated changes into separate ticket drafts |
| `type: Bug, priority: High` | Regenerates the fields with your overrides |
| any edit request | Re-drafts accordingly |

## Jira flavor note

Default output is **Jira wiki markup** (`h2.`, `{code}`, `*bold*`) — the right
paste format for Jira Server / Data Center and recognizable to most engineers.
On **Jira Cloud**, reply `cloud` to get the Markdown variant for the new editor.
The agent never talks to JIRA itself; custom fields (Sprint, custom dropdowns)
stay manual — fill them in the Jira form using the Suggested fields table as a
reference.

## Enterprise notes

- **Secret scan**: keys, tokens, connection strings, and PII-looking strings in
  the diff are flagged at the top of the reply and excluded from ticket text —
  file names only.
- **Ticket key detection**: a key like `PROJ-123` in the branch name is
  surfaced as a link/implements suggestion.
- **Mixed concerns**: if the diff contains unrelated work, it offers `split`
  instead of writing one misleading ticket.
- **Cost-aware**: bounded reads (spans, not whole files), grouped summaries for
  big diffs, cheap-model pin. Large diffs are summarized per file-group.
- **Never writes** — no files, no JIRA calls, no git mutations.

## Troubleshooting

| Problem | Fix |
|---|---|
| Agent not in dropdown | File must be in `.github/agents/`; reload the IDE |
| `/jira-*` commands missing | Prompt files live in `.github/prompts/`; JetBrains prompt support is preview |
| Handoff buttons do nothing | Install `code-reviewer` / `codebase-guide` too |
| Ticket says "inferred" a lot | Working as intended — add a line of business context and ask it to regenerate |
| Wrong scope picked | Reply with the exact SHA/range; it re-confirms stats before drafting |

## Limitations

- Details only — it cannot create, update, or comment on JIRA issues.
- Jira custom fields and board/sprint assignments remain manual.
- Intent beyond what code and commit messages show is always labeled inferred.
