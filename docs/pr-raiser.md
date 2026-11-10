# pr-raiser

Raise a pull request only after the team's traceability checklist is
satisfied. It collects the build/JIRA/documentation evidence interactively,
verifies whatever git can prove, and emits the process checklist — nothing
else: **you write the PR title, summary, and impact yourself** and paste the
checklist in. For Bitbucket Server/DC.

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
command `/pr-raise`. It asks for the merge-target branch first, then
interviews you — one question at a time.

## What it asks for

A guided interview — one question at a time, with each gate verified and
confirmed inline before the next (`✅ PB-20415 @ a1b2c3d — sha in PR range`).
Volunteering several answers at once is fine; blanks/refusals get re-asked:

| # | Question | Verified how |
|---|---|---|
| 0 | Merge-target branch | **Always asked explicitly — never assumed** (not even `main`/`master`); the repo default may be offered as a candidate but requires your confirmation. Then `git merge-base` computes the diff, and the branch must be *on top of* the target (`git merge-base --is-ancestor <target> HEAD` vs the freshest ref) — a behind/diverged branch stops the run with the missing commits and a sync instruction until you rebase and confirm |
| 1 | Private build ID **+ the commit it was triggered from** | SHA must be inside the PR's commit range — the classic wrong-build catch |
| 2 | Base build ID **+ its commit** | SHA must exist in the repo |
| 3 | Any diff in **binaries and XMLs** between base and private builds with the feature flag **disabled**? | User attestation — **screenshot of the comparison required for both `yes` and `no`** (`yes` additionally needs the differing paths/report). Refused after re-ask → blocker |
| 4 | Must Fix items? If yes, is `MustFix_Log.txt` updated? | A **commit inside `git log <target>..HEAD`** must touch it — verified per-commit with the SHA cited. An update elsewhere (other branch/repo, uncommitted) does not clear the gate: the log update travels with the merge |
| 5 | pct-documentation commit ID **+ the file path** where the change is captured | User attestation (different repo) |
| 6 | Provide the ticket link **or** open it yourself and confirm in writing | With a link the agent **fetches it via JIRA REST and verifies the contents against the diff** (needs `JIRA_TOKEN` or `~/.netrc` — see setup below); without one, your verbatim written confirmation is recorded. Unverified after re-ask → blocker |

## What you get

```markdown
## Process checklist
| Check | Evidence | Status |   ← ✅ git-verified · ✍ attested · ⚠ blocker

**Attach before raising:** <screenshot file(s)>   ← when diff evidence was collected
```

That's the whole output — the agent writes no summary, impact, or title;
you author those and paste this block into your PR description.

If a gate fails — branch behind the merge target, no JIRA key in any commit,
MustFix without the log update, private build triggered outside the PR range,
missing build info, or a required attestation refused after re-ask (diff
screenshot, JIRA-detail confirmation) — the draft is still produced but
headed `⚠ BLOCKERS — do not raise yet:` with the list.

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

### Optional: JIRA content verification setup

With a ticket link, the agent can fetch the ticket via `GET
<host>/rest/api/2/issue/<KEY>?fields=summary,description` and compare its
contents against the actual diff — real verification instead of attestation.

**Optional: store the PAT once, reuse forever** — if you want persistence
(say so during the interview), the agent guides you through storing it in
one of these stores; afterwards the lookup finds it silently and never asks
again:

Most users are on **managed Windows work laptops** (a few on macOS) — the
lookup order is `JIRA_TOKEN` env → `git credential fill` → `keyring` (if
python+keyring exist) → netrc → password-manager CLIs. Pick any store;
whichever resolves first is used silently:

**Windows (primary):**
```powershell
# 1. Credential Manager via git credential — Git for Windows ships the
#    wincred/GCM helper preconfigured, zero install. Run in Git Bash:
printf 'protocol=https\nhost=<jira-host>\nusername=<user>\npassword=<pat>\n\n' \
  | git credential approve

# 2. DPAPI-encrypted file — no install, only YOUR account can decrypt:
$pat = Read-Host -AsSecureString "JIRA PAT"
$pat | ConvertFrom-SecureString | Set-Content ~\jira-pat.sec

# 3. Persistent env var — simplest but plaintext in the registry
#    (HKCU\Environment); prefer Credential Manager/DPAPI on managed machines:
setx JIRA_TOKEN "<pat>"

# 4. %USERPROFILE%\_netrc — NOTE: underscore filename on Windows:
#    machine <jira-host> login <user> password <pat>

# 5. Python keyring, if python+keyring are installed:
python -c "import keyring; keyring.set_password('jira','pat','<pat>')"
```

**macOS (minority):**
```bash
security add-generic-password -s jira-pat -a "$USER" -w   # prompts interactively
# or the same git credential approve one-liner — osxkeychain is the default helper
```

Note: `cmdkey` alone is NOT a working store — it can create/list Windows
credentials but cannot return the secret; retrieval needs `git credential`
or `keyring` (both read Credential Manager).

The agent never runs a store/write itself — it emits the command for you to
run once (never-mutate rule). Without persistence configured it asks for
the PAT once per run and proceeds — no setup walkthrough unless you ask.

The token is **single-purpose**: wherever it comes from — env, keychain,
netrc, or pasted in chat — it authorizes only JIRA REST GETs: never echoed,
never embedded in the checklist/PR/any other command, never attached to any
other endpoint. Fetch unavailable (no creds, unreachable host, 404) → the
agent falls back to your written confirmation; nothing else changes.

## Customizing for your team

The checklist lives inside `agents/pr-raiser.agent.md` — Phase B is the
question list, Phase C the emitted table. To rename or add gates (build
naming, `MustFix_Log.txt`, `pct-documentation`, extra rows), edit those two
sections in lock-step so what's asked and what's reported stay aligned.

## Handoffs

- **jira-ticket-writer** — when the JIRA ticket exists but its content is thin
- **code-reviewer** — when you want a deeper review before raising
