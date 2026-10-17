# sharepoint-knowledge

Grounded Q&A over a SharePoint **site, library, or folder** — Site Pages
plus document libraries (`docx`/`pdf`/`pptx`/`xlsx`/`csv`/`md`/`txt`/`html`) —
indexed locally by a self-installing MCP service. Same engine as
[confluence-knowledge](confluence-knowledge.md) and
[local-knowledge](local-knowledge.md); only the connector differs.

## Auth — Entra device-code flow

One-time, per tenant (done by IT): an Entra app registration with
**delegated `Sites.Read.All`** and **"Allow public client flows"** enabled —
that's the whole requirement. No client secret is used or needed.

`sharepoint-knowledge setup` collects tenant ID, client ID, and site URL,
prints those steps again, then runs a `microsoft.com/devicelogin` sign-in
once. The MSAL token cache lives in the OS keyring (protected-file
fallback) and refreshes silently — normal use needs no re-sign-in.

- Everything is **delegated** — the index can only contain what the
  signed-in user can already read.
- If a tenant blocks public-client flows or requires admin consent that
  hasn't been granted, `setup` names the exact fix; there is no workaround
  and the agent won't invent one.

## Install / bootstrap

Same ladder as the other agents — bundled `service/` → bundled `.whl` →
`pip install sharepoint-knowledge[models]` → asks for the bundle path.
Bootstrap = install → models → `register` → `setup` (the only manual step)
→ reload.

## Scope

`connect` accepts:

- `https://tenant.sharepoint.com/sites/<name>` → Site Pages + **all** document
  libraries under that site
- `…/sites/<name>/<library or folder path>` → that subtree only
- root/teams site URLs the same way; raw `site:`/`item:`/`drive:`/`page:`
  IDs for power users

One root per `connect` — connect again for another scope.

## Crawl behaviour

- Site → Site Pages list + every drive; drives → folders → files
- Site-page canvas HTML → `extract_chunks`; file bytes → shared
  `extract_files` (the same parsers local-knowledge uses)
- Secret-looking filenames (`.env`, `*.pem`, `id_rsa`, `credentials*`…)
  excluded + disclosed as warnings; oversized files (>25 MB) disclosed
- Downloads go through `@microsoft.graph.downloadUrl` **without** the
  Bearer header — the token never leaves `graph.microsoft.com`
- GET-only client with an endpoint allowlist — no write path exists

## CLI

`sharepoint-knowledge <cmd>` — `setup`, `doctor` (config + models + update
check), `prepare-models`, `register`, `update`, `serve`, `clean`.

## Env vars

| Var | Purpose |
|---|---|
| `SP_KNOWLEDGE_HOME` | install root (`~/.sharepoint-knowledge`) |
| `SP_KNOWLEDGE_WORKDIR` | index location (`~/.sharepoint-knowledge/work`) |
| `SP_KNOWLEDGE_TOKEN` | pre-baked access token (tests/CI — bypasses MSAL) |
| `SP_KNOWLEDGE_RERANKER` | `onnx-community/bge-reranker-base-ONNX` for a faster English reranker |
| `SP_KNOWLEDGE_GRAPH_BASE` | Graph base URL override (e2e fake) |
| `SP_KNOWLEDGE_AUTH_BASE` | Entra authority override (e2e fake) |

## Roadmap

- SharePoint Server (on-prem, NTLM/auth-cookie) — the crawl/engine is
  connector-agnostic; needs a second client + auth module
- App-only client-credentials fallback for tenants that block device flow

## Verification

`mcp/sharepoint-knowledge/tests/` — GET-only enforcement, no-Bearer
downloads, site/drive/list traversal, secret disclosure, full server flow
against a fake Graph. `scripts/e2e_bootstrap_sp.py` replays fresh install
end to end against a local fake Graph server — `E2E BOOTSTRAP OK`.


## Doctor health flags

`doctor` prints a `checks` block — every red flag carries a `remediation`
(the agent follows these automatically; the table is for manual runs):

| Flag | Means | Fix |
|---|---|---|
| `deps` | an install was interrupted — a dep isn't importable | `pip install -U <cli>[models]` |
| `model_embed / model_rerank / model_rerank_light / model_sparse` | partial model download (dir exists, locked file missing) | `prepare-models` |
| `registration` | no IDE config contains the server | `register --ide auto` |
| `index:<scope>` | `corrupt` → delete index, re-crawl; `older model` → one `start_crawl` rebuild | `clean` / `start_crawl` |
| `creds_valid` | token rejected or missing (401) | `setup` again |
| `disk_free` | < 2 GB free — models won't fit | free space |
| `python` | venv interpreter < 3.11 | rebuild venv on 3.11+ |
| `proxy` | informational — shows HTTPS_PROXY/HTTP_PROXY visibility | — |

`doctor` exits non-zero while any flag is red.
