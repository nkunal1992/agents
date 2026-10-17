# sharepoint-knowledge — Copilot agent bundle

Grounded Q&A over a **SharePoint site, library, or folder** — Site Pages +
docx/pdf/pptx/xlsx/csv/md/txt/html — via GitHub Copilot custom agents + a
local MCP service. Crawl, index, and embeddings run on the user's machine;
the only remote calls are the read-only SharePoint fetches themselves.

## What it is

A Copilot agent (`sharepoint-knowledge.agent.md`) plus a small Python MCP
service it installs itself on first use. Point it at a SharePoint site or
folder → it crawls what *the signed-in user* can already see → answers with
citations back to the exact page/document, and says "not documented"
instead of guessing.

## Why teams use it

| Benefit | How |
|---|---|
| Answers you can verify | every claim carries a SharePoint URL citation |
| Refuses instead of inventing | missing info → explicit "not documented" |
| Respects permissions | delegated `Sites.Read.All` — sees only what the user already can |
| Modern enterprise auth | Entra device-code sign-in — no PATs, no app secrets to rotate |
| Reads real office files | docx/pptx/xlsx/pdf/csv/md/txt/html, not just site pages |
| Safe by default | secret-looking filenames excluded + disclosed; GET-only client |

## Enterprise requirements (read before rollout)

1. **An Entra app registration** — once per tenant, done by IT:
   - App registrations → new (accounts in this org)
   - Authentication → *Allow public client flows* = **Yes** (device code)
   - API permissions → Graph → **Delegated** → `Sites.Read.All` → admin consent
2. Python ≥ 3.11, GitHub Copilot with custom-agent support, ~300 MB disk.
3. `setup` prints these steps again for whoever runs it.

## Install

Copy `sharepoint-knowledge.agent.md` into `.github/agents/` of any repo.
First use: the agent installs the service itself (bundled `service/` →
bundled `.whl` → `pip install sharepoint-knowledge[models]` → asks for the
bundle path), downloads the models, writes the IDE MCP config. Then the user
runs `sharepoint-knowledge setup` once in their terminal (tenant ID, client
ID, site URL → microsoft.com/devicelogin sign-in). One reload — done.

## Use

Select **sharepoint-knowledge** in Copilot Chat, then:

- *"what does the release checklist say in https://tenant.sharepoint.com/sites/eng?"* — whole site (pages + libraries)
- *"summarize the audit docs under …/sites/finance/Shared Documents/audit/"* — folder subtree

Refresh = same URL again; changed items update, deleted items drop out.

## Security & privacy

- Token cache in the OS keyring (protected-file fallback); silent refresh —
  one sign-in lasts.
- GET-only client with a route allowlist — it physically cannot write to
  SharePoint. Downloads use Graph's own pre-signed `downloadUrl`, so the
  bearer token never leaves `graph.microsoft.com`.
- Secret-looking filenames (`.env`, `*.pem`, `credentials*`…) are excluded
  and disclosed as crawl warnings.
- Retrieval + reranking on pinned local models — document text is never
  sent to a cloud embedding API.

## Troubleshooting

| Symptom | Fix |
|---|---|
| sign-in fails "admin consent required" | IT grants consent on the app registration — not user-fixable |
| `device code` never appears | "Allow public client flows" isn't enabled on the app |
| a page/folder missing from answers | the signed-in user can't see it either, or it was skipped — check crawl warnings |
| ARM64 Windows | install the x64 python.org build; torch lacks win-arm64 wheels |

## Uninstall

```bash
<venv>/bin/sharepoint-knowledge register --remove
rm -rf ~/.sharepoint-knowledge
```
Then have IT delete the app registration if it was created just for this.
