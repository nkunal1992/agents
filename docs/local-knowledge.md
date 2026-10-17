# local-knowledge

Grounded Q&A over a local **file or folder** — `docx`, `pdf`, `pptx`,
`xlsx`, `csv`, `md`, `txt`, `html` — indexed locally on the user's machine
by a self-installing MCP service. Same engine as
[confluence-knowledge](confluence-knowledge.md), pointed at the filesystem.

**No credentials.** There is no `setup` command — the filesystem is the
access control (the service can only read what the OS already lets the user
read).

## Install

Copy `agents/local-knowledge.agent.md` into `.github/agents/` of any repo.
First use: the agent installs the service itself (bundled `service/` →
bundled `.whl` → `pip install local-knowledge[models]` → asks for the bundle
path, in that order), downloads the local models (~1.3 GB once), writes the IDE MCP
config, and asks for one reload.

Manual install (same commands the agent runs):

```bash
python3 -m venv ~/.local-knowledge/venv
~/.local-knowledge/venv/bin/python -m pip install "local-knowledge[models]"
~/.local-knowledge/venv/bin/local-knowledge prepare-models   # ~1.3 GB, once
~/.local-knowledge/venv/bin/local-knowledge register --ide auto --workspace "$PWD"
~/.local-knowledge/venv/bin/local-knowledge doctor           # verify
```

## Use

In Copilot Chat select **local-knowledge**, then:

- *"what's in `~/Documents/runbooks/`?"* — folder → recursive index
- *"summarize `~/specs/migration.pdf`"* — file → single-document index

`connect` takes either a file or a folder path — it detects which. Refresh
is the same command again; changed files update, deleted files drop out.

Citations are `file://` absolute paths — clickable in most IDEs.

## Safety defaults

- **Secret-looking files are excluded by default** — `.env*`, `id_rsa`,
  `id_dsa`, `id_ed25519`, `id_ecdsa`, `*.pem`, `*.key`, `*.p12`, `*.pfx`,
  `*.keystore`, `*.jks`, `credentials*`, `.netrc`, `.pgpass` — and every
  exclusion is disclosed in the crawl's `warnings`, never silent. (Naming
  `connect` *directly* at such a file is the explicit override — you asked.)
- Symlinks that resolve **outside** the chosen root are skipped and
  disclosed.
- Read-only — the service never writes to the indexed tree; its index lives
  in `~/.local-knowledge/work` (or `LOCAL_KNOWLEDGE_WORKDIR`).
- Size caps: files over 25 MB and folder scans over 20k files are
  disclosed-and-skipped, not crashes.

## CLI

`local-knowledge <cmd>` — same verbs as the Confluence service minus `setup`:

| Command | Purpose |
|---|---|
| `prepare-models` | download the pinned embed + rerank models |
| `register [--ide auto\|all\|vscode\|jetbrains] [--workspace <dir>] [--remove]` | write/remove IDE MCP config (merges, backs up first) |
| `doctor` | report install/model health |
| `serve` | run the stdio MCP server (what the IDE launches) |
| `clean` | delete the local index |

## Env vars

| Var | Purpose |
|---|---|
| `LOCAL_KNOWLEDGE_HOME` | installation root (default `~/.local-knowledge`) |
| `LOCAL_KNOWLEDGE_RERANKER` | `onnx-community/bge-reranker-base-ONNX` for a faster English reranker |
| `LOCAL_KNOWLEDGE_WORKDIR` | index location (default `~/.local-knowledge/work`) |

## Maintainers: publishing

Tag `local-knowledge-v*` → `.github/workflows/publish-pypi.yml` builds and
uploads to PyPI (same `PYPI_API_TOKEN` secret as confluence-knowledge).
`scripts/bundle-service.sh local --zip` produces the store bundle.

## Verification

`service/scripts/e2e_bootstrap_local.py` replays a fresh install end to end
against a generated doc tree — real venv, real models, real stdio MCP —
finishing with `E2E BOOTSTRAP OK`. No network or fake server needed.


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
