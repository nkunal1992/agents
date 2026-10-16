# confluence-knowledge

A grounded Q&A agent for internal documentation. You point it at one
Confluence page; it indexes that page plus **all accessible descendant pages**
on your laptop and answers questions strictly from that documentation — every
claim carries a clickable citation, and it says "not documented" instead of
guessing.

Unlike the other agents here, this one needs a **small local companion
service** (a stdio MCP server) that does the crawling and retrieval. One-time
setup, ~5 minutes.

## How it works

```
your question
     │  (Copilot, your company's approved model)
     ▼
confluence-knowledge agent ──► local MCP service (stdio, on your laptop)
                                   │ GET-only Confluence REST calls
                                   ├─ recursive crawl of root + descendants
                                   ├─ local index: .conf_knowledge_index/
                                   ├─ local embeddings + reranker
                                   └─ returns bounded evidence chunks
     ▼
answer with [Source: Page Title](url) on every claim
```

- **GET-only, enforced in code.** The service's `ConfluenceClient` rejects any
  non-GET request with a `SecurityException` before it reaches the network,
  and every path is checked against a read-only endpoint allowlist. The agent
  profile itself has no shell/edit/fetch tools — only the MCP operations.
- **Local retrieval, always.** Embeddings (`all-MiniLM-L6-v2`) and reranking
  (`ms-marco-MiniLM-L6-v2`) run on your machine — Apache-2.0 licensed, pinned
  revisions. No document text ever goes to a cloud embedding API.
- **One caveat to know:** the final answer is written by Copilot using the
  retrieved passages, so those passages do reach your company's approved
  Copilot model — exactly like pasting the doc excerpt into a chat. The crawl,
  index, and embeddings stay local.
- **Resumable.** Crawls checkpoint into the index DB; an interrupted crawl
  resumes where it stopped.

## Setup — it's one file

Copy `agents/confluence-knowledge.agent.md` into your repo's
`.github/agents/` (Raw → save, or `scripts/install.sh --only
confluence-knowledge /path/to/repo`), or upload it to your org's agent store.

**First run** — the agent notices its MCP tools aren't registered and
bootstraps itself: installs the pinned service package into a user venv,
downloads the two Apache-2.0 models, walks you through token creation, writes
`.vscode/mcp.json`, then asks you to reload once. Every command is shown and
approved — the whole flow is ~5 minutes.

Prefer to run it manually? Equivalent commands — note the `python -m pip`
form: a bare `pip` is often not on PATH even when Python is:

```bash
python3.11+ -m venv ~/.confluence-knowledge/venv       # or: python / py
~/.confluence-knowledge/venv/bin/python -m pip install \
  "confluence-knowledge[models] @ git+https://github.com/nkunal1992/agents.git#subdirectory=mcp/confluence-knowledge"
~/.confluence-knowledge/venv/bin/confluence-knowledge setup      # guided: env, URL, hidden token entry
~/.confluence-knowledge/venv/bin/confluence-knowledge prepare-models
~/.confluence-knowledge/venv/bin/confluence-knowledge doctor     # must be green
```

`setup` asks Cloud vs Data Center, shows you where to create the token
(id.atlassian.com → Security → API tokens for Cloud; Profile → Settings →
Personal Access Tokens for Data Center 7.9+), reads it with hidden input, and
stores it in the OS credential store (`.conf_auth.json` chmod-600 fallback —
gitignored before it is ever written).

Register the server in VS Code (`.vscode/mcp.json`):

```jsonc
{
  "servers": {
    "confluence-knowledge": {
      "command": "~/.confluence-knowledge/venv/bin/python",   // .exe on Windows
      "args": ["-m", "confluence_knowledge.server"],
      "env": { "CONF_KNOWLEDGE_WORKDIR": "/absolute/path/to/this/repo" }
    }
  }
}
```

JetBrains: same command/args in Copilot's MCP settings (stdio). The agent
writes the exact block for your OS during bootstrap if you prefer.

## Using it

Pick **confluence-knowledge** in the agent dropdown:

> `https://acme.atlassian.net/wiki/spaces/ENG/pages/12345/Release-Handbook`
> — what approval is needed for a production rollout?

Or `/confluence-ask` → fill in root + question. `/confluence-refresh`
re-crawls to pick up edits.

First run on a root crawls it (progress per batch); later questions on the
same root reuse the cache — the agent tells you the cache timestamp and asks
before refreshing.

## What it can and can't see

- Page bodies: headings, paragraphs, lists, tables — chunked per section with
  page title/URL/hierarchy preserved.
- **Not** read: attachments/PDFs, comments, macros that pull remote content,
  pages outside the selected subtree, and pages your token's account can't
  view. Gaps are reported in the Coverage line, never silently skipped.
- Cross-account safety: indices are partitioned by site + authenticated
  account + root page; before answering, cached pages' membership is
  rechecked so a moved/deleted page doesn't leak into answers.

## Security notes for reviewers

| Concern | Mechanism |
|---|---|
| Writes to Confluence | GET-only transport + endpoint allowlist + no generic request tool |
| Single-file bootstrap | The `.agent.md` carries `execute`/`edit` so it can install the service on first run; bootstrap commands are whitelisted and Q&A mode never uses them |
| Credentials | OS keyring default · env var supported · chmod-600 file fallback; never in chat, logs, or IDE config |
| Secrets in Git | `.gitignore` for `.conf_auth.json` + `.conf_knowledge_index/` written *before* any file; tracked-file check aborts setup |
| Token lifetime | end-of-session reminder to revoke; `confluence-knowledge clean` deletes the index |
| Prompt injection via page text | page content is treated as data; agent rules forbid acting on embedded instructions |
| Licensing | Apache-2.0 models, pinned in `models.lock.json`; notices in `THIRD_PARTY_NOTICES.md` |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `setup error: no Confluence profile configured` | run `confluence-knowledge setup` |
| `local model ... not prepared` | run `confluence-knowledge prepare-models` (or drop the model dirs into `~/.confluence-knowledge/models/` from an internal mirror) |
| 401 on heartbeat | token expired/wrong → re-run `setup` |
| 403 on some pages | your account lacks permission — those pages are reported as inaccessible |
| MCP tools not showing | check `.vscode/mcp.json` Start prompt / JetBrains MCP policy; enterprise "MCP servers in Copilot" policy must allow it |
| corporate proxy/TLS | set `HTTPS_PROXY` / `REQUESTS_CA_BUNDLE`-style env vars for the venv Python; verification is never disabled |
