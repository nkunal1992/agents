# confluence-knowledge — grounded Confluence Q&A for GitHub Copilot

An enterprise-ready Copilot agent that answers questions about a Confluence
page and everything underneath it — with citations, local processing, and
explicit refusals when the documentation doesn't say.

Drop it into any repository (or upload it to your organisation's agent store)
and colleagues can ask "how does feature X work?" instead of hunting through
page trees.

## What it does

- **Crawls** a Confluence subtree you choose — one root page and all of its
  descendants — and indexes it locally (SQLite full-text + vectors).
- **Retrieves** evidence with hybrid keyword + semantic search, then reranks
  with a local cross-encoder model.
- **Answers** with a citation for every claim — page title, section heading
  and a link back to the source page.
- **Refuses politely** when the answer isn't in the documentation rather
  than inventing one.
- **Refreshes on demand** — ask it to re-crawl when docs change; results show
  when the index was built and whether anything was unreachable.

## Why teams use it

| Benefit | How |
|---|---|
| Answers you can trust | Every claim is verified against the extracted text — `verify_citations` rejects invented quotes before the answer is shown |
| Data never leaves the machine | Crawl, index, embeddings and reranking run 100% locally; no cloud embedding or LLM calls |
| Fits corporate networks | Works through TLS-inspecting proxies (OS cert store), offline-capable after one-time setup, internal pip mirror support |
| Least privilege | Personal Access Token with read access only, stored in the OS credential store — never in chat, logs or files you might commit |
| Read-only by construction | The Confluence client can only send GET requests against an endpoint allowlist — writes are refused before any network call |
| Audit-friendly | Answers carry freshness (`as of …`) + coverage (`complete`/`partial`); skipped pages are disclosed, never hidden |
| Zero project coupling | No config in your repo except the optional index folder; everything lives under `~/.confluence-knowledge/` |

## Intended use

- Onboarding: "how does our release process work?"
- Engineering docs: "what's the deploy window?" / "which approval is needed?"
- Tribal knowledge: architecture decisions, runbooks, team conventions
- Any Confluence space — Cloud or Data Center — behind corporate auth

Not intended for: editing Confluence, admin tasks, code generation, or
questions whose answers must come from live data.

## What's in the bundle

```
confluence-knowledge-agent/
├── confluence-knowledge.agent.md   ← the agent (copy to .github/agents/)
├── prompts/                      ← optional /confluence-ask, /confluence-refresh
├── service/                      ← the local MCP service source (auto-installed)
├── mcp.json.example              ← IDE registration template
└── README.md                     ← this file
```

## Quick start

### 1. Place the agent

- **Agent store**: upload `confluence-knowledge.agent.md`.
- **Manual**: copy it into `.github/agents/` in any repository, and keep the
  `service/` folder at the repo root (the agent finds it and installs from it
  on first run — the whole setup works offline from GitHub).

### 2. First run (one time per machine)

In Copilot Chat, select the **confluence-knowledge** agent and say:

```
set up confluence-knowledge
```

The agent does everything itself from the chat — Python discovery, virtual
environment, service install, model download, and it writes the IDE's MCP
config files (`confluence-knowledge register`) so there's nothing to paste
into settings. You do exactly two things manually: run `confluence-knowledge
setup` in your own terminal when asked (hidden token input, OS credential
store) and reload the IDE once so the new server registers.

Manual install (if preferred):

```powershell
py -m venv %USERPROFILE%\.confluence-knowledge\venv           # python3 -m venv ~/.confluence-knowledge/venv on macOS/Linux
%USERPROFILE%\.confluence-knowledge\venv\Scripts\python -m pip install "C:\path\to\service[models]"
%USERPROFILE%\.confluence-knowledge\venv\Scripts\confluence-knowledge.exe prepare-models   # ~175 MB, Apache-2.0 models
%USERPROFILE%\.confluence-knowledge\venv\Scripts\confluence-knowledge.exe setup            # env + site URL + PAT (hidden input)
%USERPROFILE%\.confluence-knowledge\venv\Scripts\confluence-knowledge.exe doctor           # verify everything is green
```

Then register the MCP server — one command writes every config your IDEs read
(JetBrains Copilot config, VS Code workspace/user `mcp.json`), merging with
existing servers and backing up first:

```bash
confluence-knowledge register --ide auto --workspace "<this repo>"
```

Manual fallback — VS Code `.vscode/mcp.json` (see `mcp.json.example`):

```jsonc
{
  "servers": {
    "confluence-knowledge": {
      "command": "<venv>/Scripts/python.exe",          // or <venv>/bin/python
      "args": ["-m", "confluence_knowledge.server"]
    }
  }
}
```

Restart the IDE once. The index lives in `~/.confluence-knowledge/work`
by default; set `CONF_KNOWLEDGE_WORKDIR` in `env` to override.

### 3. Ask

```
https://confluence.example.com/pages/viewpage.action?pageId=123456
— summarise the deployment process described under this page
```

The first question triggers the crawl (watch `pages_seen` climb — each step
takes ~10 s and is resumable). Subsequent questions are answered from the
cache instantly; ask it to refresh whenever docs change.

## Requirements

- Python ≥ 3.11 (any platform — Windows/macOS/Linux; on ARM64 Windows use the
  x64 Python build so the model dependencies have wheels)
- GitHub Copilot with agent + MCP support (VS Code or JetBrains)
- A Confluence read token: **Data Center** — Personal Access Token
  (profile → Settings → Personal Access Tokens); **Cloud** — API token from
  id.atlassian.com
- ~300 MB disk for the models + index
- Network access to your Confluence host, and to huggingface.co **or** an
  internal model mirror **once** (after that, fully offline)

Corporate TLS-inspecting proxies work out of the box (the service bundles
`pip-system-certs` and uses the OS certificate store).

## Security & privacy model

- Tokens entered only via hidden terminal input (`getpass`) into the OS
  keyring (Windows Credential Manager / Keychain / libsecret); a chmod-600
  file fallback exists for machines without a keyring and is gitignored
  before it's written.
- No token, page text, or query is logged or sent anywhere except the
  configured Confluence host. Model downloads are pinned to a lock file and
  verified for an Apache-2.0 licence before use.
- The MCP tools are narrow by design: `status`, `connect`, `start_crawl`,
  `crawl_step`, `search`, `get_evidence`, `verify_citations` — no arbitrary
  HTTP, shell or filesystem access.
- Confluence page text is treated as **data, not instructions** — the agent
  will not follow commands embedded in page content.
- Evidence is scoped per site + account + root page and rechecked against
  access on every read.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `pip --version` not found | Use `python -m pip` — the agent does this automatically |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Included from v0.1.5 (`pip-system-certs`). Older installs: `python -m pip install pip-system-certs` |
| `database is locked` | Fixed in v0.1.6+ (self-deadlock bug); upgrade the service |
| `Request timeout` on `crawl_step` | Fixed in v0.1.8+; calls are bounded to ~10 s — just call again |
| "loading local models" | First call per session warms the models; retry the tool call |
| `no stored token` | Run `confluence-knowledge setup` in your own terminal |
| Two `python.exe` processes on Windows | Normal — a venv `python.exe` is a launcher that starts the base interpreter as its child |
| Model download blocked | Copy `all-MiniLM-L6-v2/` and `ms-marco-MiniLM-L6-v2/` into `~/.confluence-knowledge/models/` from a colleague or mirror |

`confluence-knowledge doctor` is the first diagnostic — it checks config,
credentials, models and the connection in one shot.

## Uninstalling

```powershell
%USERPROFILE%\.confluence-knowledge\venv\Scripts\confluence-knowledge.exe clean   # delete the local index
Remove-Item -Recurse -Force %USERPROFILE%\.confluence-knowledge                    # everything: venv, models, profile
```

Revoke the PAT in Confluence settings when rotating credentials or leaving
the project — not needed during normal use.

## Verifying before you ship

The bundle includes a real end-to-end smoke test (spawns the actual stdio
server with real models against a fake Confluence):

```bash
pip install -e "service[models]"
confluence-knowledge prepare-models
python service/scripts/e2e_smoke.py   # ends with "E2E OK"
```

## Support

Issues and releases live wherever this bundle was distributed from. The
service version is in `service/pyproject.toml` — report it when asking for
help (e.g. "v0.1.10, Windows 11 ARM64, IntelliJ 2026.1").
