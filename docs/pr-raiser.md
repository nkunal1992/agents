# pr-raiser

Raise a pull request only after the team's traceability checklist is
satisfied. It collects the build/JIRA/documentation evidence interactively,
verifies whatever git can prove, assesses the change read-only, and emits a
paste-ready PR title and description for Bitbucket Server/DC — with the
process checklist embedded in the PR itself.

- **Enforces the process every time**: the same evidence is collected on every
  PR — builds, binary/XML diff, MustFix, documentation commit, JIRA — so
  compliance doesn't depend on reviewer vigilance.
- **Verifies, doesn't just ask**: JIRA keys, `MustFix_Log.txt` presence, and
  whether the private-build commit is actually inside the PR's range are
  checked in git, not taken on faith. User-only facts are recorded verbatim —
  never guessed.
- **Never mutates**: cannot edit files, commit, merge, rebase, or push — and
  never creates or merges the PR itself (no CLI, no API). Raising the PR is
  your act; the agent produces the content. Shell access is whitelisted to
  read-only git plus `git fetch` for ref freshness.
- **Honest gating**: unanswered or failed checks render as `⚠` blockers in the
  draft — a padded ✅ is never produced.

## Quick start

```bash
scripts/install.sh --only pr-raiser /path/to/your-repo
# optional, powers the handoff buttons:
scripts/install.sh --only pr-raiser --only jira-ticket-writer --only code-reviewer /path/to/your-repo
```

Pick **pr-raiser** in the Copilot Chat agent dropdown, or use the slash
command `/pr-raise`. It asks for the merge-target branch first, then collects
everything else in one message.

## What it asks for

Answer these in one reply — the agent re-asks only what comes back blank:

| # | Question | Verified how |
|---|---|---|
| 0 | Merge-target branch | `git merge-base` — required to compute the diff; the branch must then be *on top of* the target (`git merge-base --is-ancestor <target> HEAD` vs the freshest ref) — a behind/diverged branch stops the run with the missing commits and a sync instruction until you rebase and confirm |
| 1 | Private build ID **+ the commit it was triggered from** | SHA must be inside the PR's commit range — the classic wrong-build catch |
| 2 | Base build ID **+ its commit** | SHA must exist in the repo |
| 3 | Any diff in **binaries and XMLs** between base and private builds with the feature flag **disabled**? | User attestation — the agent can't reach your build system |
| 4 | Must Fix items? If yes, is `MustFix_Log.txt` updated? | File presence in the diff, or an attested path elsewhere |
| 5 | pct-documentation commit ID **+ the file path** where the change is captured | User attestation (different repo) |
| 6 | Does the JIRA ticket describe what changed? | Keys auto-verified in commit messages; ticket *contents* attested by you |

## What you get

```markdown
## Summary            change grouped by concern (feature/fix/refactor/test/config)
## Impact             systems touched, compat/migration surface, flag gating
## Change assessment  efficiency verdict — clean / acceptable-with-notes / needs-attention
                      + recommendations, flagged never applied, only when warranted
## Process checklist  | Check | Evidence | Status | — ✅ git-verified · ✍ attested · ⚠ blocker
```

If a gate fails — branch behind the merge target, no JIRA key in any commit,
MustFix without the log update, private build triggered outside the PR range,
missing build info — the draft is still produced but headed `⚠ BLOCKERS —
do not raise yet:` with the list.

### Follow-up replies

| Reply | Result |
|---|---|
| `edit` / describe a change | Revise that field and re-emit — no full re-interview |
| `create` | After you've raised it — the agent never calls the API itself |

### Optional: create via Bitbucket Server REST

The agent emits a ready-to-fill block you run yourself — it never holds
credentials:

```bash
curl -u <user>:<pass> -H "Content-Type: application/json" -X POST \
  "https://<bb-host>/rest/api/1.0/projects/<PROJ>/repos/<REPO>/pull-requests" \
  -d '{"title":"…","description":"…","fromRef":{"id":"refs/heads/<src>"},"toRef":{"id":"refs/heads/<dst>"}}'
```

## Customizing for your team

The checklist lives inside `agents/pr-raiser.agent.md` — Phase B is the
question list, Phase D the emitted table. To rename or add gates (build
naming, `MustFix_Log.txt`, `pct-documentation`, extra rows), edit those two
sections in lock-step so what's asked and what's reported stay aligned.

## Handoffs

- **jira-ticket-writer** — when the JIRA ticket exists but its content is thin
- **code-reviewer** — when you want a deeper review before raising
