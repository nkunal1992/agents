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
- **Local retrieval, always.** Embeddings (`bge-m3`, quantized ONNX) and reranking
  (`bge-reranker-v2-m3`, quantized ONNX) run on your machine — MIT/Apache-2.0 licensed, pinned
  revisions. No document text ever goes to a cloud embedding API.
- **One caveat to know:** the final answer is written by Copilot using the
  retrieved passages, so those passages do reach your company's approved
  Copilot model — exactly like pasting the doc excerpt into a chat. The crawl,
  index, and embeddings stay local.
- **Resumable.** Crawls checkpoint into the index DB; an interrupted crawl
  resumes where it stopped.

## Setup — it's one file (or one zip)

Copy `agents/confluence-knowledge.agent.md` into your repo's
`.github/agents/` (Raw → save, or `scripts/install.sh --only
confluence-knowledge /path/to/repo`), or upload it to your org's agent store.

**Zip bundle (AI store / no GitHub access):** run
`scripts/bundle-confluence.sh --zip` to produce
`dist/confluence-knowledge-bundle.zip` containing the agent file, prompts,
the vendored `service/` source, an `mcp.json.example`, and a README. Unzip
into the repo — the agent detects `service/` on first run and installs from
it (`pip install ./service[models]`) instead of GitHub. Python deps still
come from PyPI (set `PIP_INDEX_URL` for an internal index, or vendor wheels
with `--include-wheels <dir>`); models come from Hugging Face or your mirror.
The service itself installs from whichever source is available — bundled
`service/`, the prebuilt wheel in the zip, or the `confluence-knowledge`
package on PyPI / your internal mirror.

**Publishing to PyPI (maintainer):** create a project on pypi.org, then
either add trusted publishing (repo + `publish-pypi.yml`) or set a
`PYPI_API_TOKEN` repository secret. Tagging `confluence-knowledge-v*` runs
`.github/workflows/publish-pypi.yml` → `python -m build` → upload.

**First run** — the agent notices its MCP tools aren't registered and
bootstraps itself: installs the pinned service package into a user venv,
downloads the two Apache-2.0 models, writes the IDE MCP config itself
(`register` — JetBrains user config + `.vscode/mcp.json`, no pasting into
settings). The only manual bits: `setup` once in your own terminal for the
token, and one IDE reload.

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

Register the server — one command writes every config Copilot reads
(JetBrains `github-copilot/intellij/mcp.json`, VS Code user + workspace):

```bash
~/.confluence-knowledge/venv/bin/confluence-knowledge register --ide auto \
    --workspace "<this repo>"        # use --remove to undo
```

Manual fallback — `.vscode/mcp.json`:

```jsonc
{
  "servers": {
    "confluence-knowledge": {
      "command": "~/.confluence-knowledge/venv/bin/python",   // .exe on Windows
      "args": ["-m", "confluence_knowledge.server"]
    }
  }
}
```

The index lives in `~/.confluence-knowledge/work` by default, shared by the CLI
and the server. Set `"env": { "CONF_KNOWLEDGE_WORKDIR": "<folder>" }` only to
put it elsewhere. For a faster English-only reranker on slow machines, set
`"env": { "CONF_KNOWLEDGE_RERANKER": "onnx-community/bge-reranker-base-ONNX" }`.

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

## Updating safely

`confluence-knowledge update` is deliberately conservative:

- **Only soaked releases** — a release must sit on PyPI for 3 days before
  it's offered (`confluence-knowledge update --latest` skips the soak; `--to <ver>` pins
  exactly). A just-published version shows as "soaking until <date>" in
  doctor — that's the intended delay, not a bug.
- **Self-test after install** — it runs `import server → tool list →
  health checks → a real mini-RAG` on the new code before declaring
  success.
- **Automatic rollback** — a `pip freeze` snapshot goes to
  `<home>/rollback/`; any stage failing restores the previous environment
  and re-verifies it. `confluence-knowledge update --rollback` restores the last
  snapshot manually. The last 3 snapshots are kept.
- **Yanked releases** — never installable. If you're on one, doctor shows
  a `release` flag and `update` moves you to the last-good release even
  when that's a downgrade.
- **Quit the IDE first** — a running MCP server holds the old code open.
  On Windows a running exe can't replace itself, so `update` prints the
  equivalent `venv-python -m <cli_module> update` command to run instead.
- `confluence-knowledge selftest` — the same checks on demand ("is my install actually
  working?"), exit 0/1.

Env knobs: `KNOWLEDGE_UPDATE_INDEX` (mirror URL template),
`KNOWLEDGE_UPDATE_SOAK_DAYS` (default 3 — 0 disables the delay).

## Security notes for reviewers

| Concern | Mechanism |
|---|---|
| Writes to Confluence | GET-only transport + endpoint allowlist + no generic request tool |
| Single-file bootstrap | The `.agent.md` carries `execute`/`edit` so it can install the service on first run; bootstrap commands are whitelisted and Q&A mode never uses them |
| Credentials | OS keyring default · env var supported · chmod-600 file fallback; never in chat, logs, or IDE config |
| Secrets in Git | `.gitignore` for `.conf_auth.json` + `.conf_knowledge_index/` written *before* any file; tracked-file check aborts setup |
| Token lifetime | stays in the OS credential store between sessions; `confluence-knowledge clean` deletes index + token when uninstalling |
| Prompt injection via page text | page content is treated as data; agent rules forbid acting on embedded instructions |
| Licensing | Apache-2.0 models, pinned in `models.lock.json`; notices in `THIRD_PARTY_NOTICES.md` |

## Doctor health flags

`doctor` prints a `checks` block — every red flag carries a `remediation`
(the agent follows these automatically; the table is for manual runs):

| Flag | Means | Fix |
|---|---|---|
| `deps` | an install was interrupted — a dep isn't importable | the pinned reinstall doctor prints — `pip install "confluence-knowledge[models]==<installed>"` |
| `model_embed / model_rerank / model_rerank_light / model_sparse` | partial model download (dir exists, locked file missing) | `prepare-models` |
| `registration` | no IDE config contains the server | `register --ide auto` |
| `index:<scope>` | `corrupt` → delete index, re-crawl; `older model` → one `start_crawl` rebuild | `clean` / `start_crawl` |
| `creds_valid` | token rejected or missing (401) | `setup` again |
| `disk_free` | < 2 GB free — models won't fit | free space |
| `python` | venv interpreter < 3.11 | rebuild venv on 3.11+ |
| `proxy` | informational — shows HTTPS_PROXY/HTTP_PROXY visibility | — |
| `resources` | shows the resource limits + running server count; red when >4 knowledge servers run | close unused IDE windows (idle servers unload models anyway) |

`doctor` exits non-zero while any flag is red.

## Resource usage

The server is designed to stay out of the way on a developer laptop:

- **Models load lazily** on first `connect` (not at process start) and
  **unload after ~10 idle minutes** — an idle server uses ~0.2 GB RSS,
  an active one ~1.9 GB. Every IDE window starts its own server process;
  `doctor`'s `resources` flag warns when more than 4 are running
  (close unused windows — idle ones unload anyway).
- **Bounded inference**: embedding/reranking run in micro-batches whose
  attention cost (items × longest-input²) stays under
  `KNOWLEDGE_BATCH_TOKENS` (default 2.5 M) and `KNOWLEDGE_MAX_BATCH`
  (default 16) — a 2048-token page now peaks ≈ +1 GB per run instead of
  padding a whole batch to it.
- **Capped threads**: each inference session uses `KNOWLEDGE_THREADS`
  (default ≈ cores/4, max 4); busy-wait spinning is off.
- **Lowered OS priority**: the process runs at nice +10 (POSIX) /
  below-normal (Windows) so interactive work always wins the CPU —
  `KNOWLEDGE_NO_NICE=1` opts out.
- `KNOWLEDGE_IDLE_UNLOAD_S` sets the idle timeout in seconds
  (0 disables unloading).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `setup error: no Confluence profile configured` | run `confluence-knowledge setup` |
| `SSL: CERTIFICATE_VERIFY_FAILED` (Confluence or huggingface.co) | v0.1.5+ ships `pip-system-certs` so requests/httpx use the OS cert store — should be automatic. On older installs: `python -m pip install pip-system-certs`, or point `REQUESTS_CA_BUNDLE` at your corporate root CA |
| `local model ... not prepared` | run `confluence-knowledge prepare-models` (or drop the model dirs into `~/.confluence-knowledge/models/` from an internal mirror) |
| 401 on heartbeat | token expired/wrong → re-run `setup` |
| `index database was locked` | fixed in v0.1.6. Earlier versions deadlocked themselves on the first crawl (an uncommitted write per connection). Upgrade: `python -m pip install --upgrade "<bundle>\service[models]"`, restart the IDE |
| `MCP error -32001: Request timeout` on `crawl_step` | fixed in v0.2.1: the crawl runs in a background thread server-side and `crawl_step` is an instant progress poll — a request timeout can no longer stall or lose a crawl; progress checkpoints continuously and a server restart auto-resumes |
| fan loud / laptop sluggish during a crawl | `doctor` `resources` flag lists limits; close extra IDE windows (each runs its own server), set `KNOWLEDGE_THREADS=1`, or use the light reranker (`CONFLUENCE_KNOWLEDGE_RERANKER=onnx-community/bge-reranker-base-ONNX`) |
| `search` says "crawl … still running" | the background crawl hasn't finished; poll `crawl_step` for progress. The index is published only when the whole subtree is crawled |
| `crawl_step` returns `state: abandoned` | the job was superseded by a newer crawl or its resume was fenced (e.g. model upgrade changed vector dims) — it can never finish; call `start_crawl` for a fresh job |
| Two `python.exe … confluence_knowledge.server` processes on Windows | expected, not a duplicate: a venv's `Scripts\python.exe` is a launcher that starts the base interpreter as its child. Only one server is running |
| 403 on some pages | your account lacks permission — those pages are reported as inaccessible |
| MCP tools not showing | check `.vscode/mcp.json` Start prompt / JetBrains MCP policy; enterprise "MCP servers in Copilot" policy must allow it |
| corporate proxy/TLS | set `HTTPS_PROXY` / `REQUESTS_CA_BUNDLE`-style env vars for the venv Python; verification is never disabled |

## Retrieval limits (by design)

- Evidence text returned by `search` is capped at 12000 chars per chunk;
  `truncated: true` flags it — `get_evidence` returns the full text.
  `get_evidence(page_id=…)` lists a whole page's chunks in ordinal order —
  how the Failure Hunt boundary probe fetches first/last chunks. Evidence
  items carry `page_id`, `kind`, `ordinal` and `page_version`.
- `search` returns an `evidence_set` handle — `verify_citations` requires
  it. Sets expire when a new crawl publishes ("predates the latest
  published snapshot") — re-run `search` for a fresh one.
- FTS indexes at most the first 16 whitespace-separated query terms;
  the dense + sparse legs carry longer questions.
- Evidence scoring below ~0.3 rerank probability is flagged
  `low_confidence: true`; below ~0.02 nothing is returned at all.
- Whole-scope search caps at 100k chunks — connect a narrower scope on
  bigger indexes.
- The sparse leg is word-piece based: it adds little for CJK content
  (dense + FTS still work); multilingual quality lives mostly in the
  dense model.
- `scripts/eval_rag.py` runs the opt-in real-model regression eval —
  `CK_MODEL_HOME=<prepared-dir> python scripts/eval_rag.py`.

## Benchmarking (golden set)

`scripts/eval_rag.py` runs a golden-set regression eval against a live index
(`--index <scope.db>`) or the built-in test corpus:

    CK_MODEL_HOME=<models-dir> python scripts/eval_rag.py \
        --golden scripts/golden_set.sample.json --index <scope.db> \
        --report eval_report.json

Metrics: recall@8, required-fact coverage, no-answer honesty, p50/p95
latency; exits non-zero below thresholds (CI e2e gates on this).

**Judging with Copilot**: the judge persona lives at
`prompts/rag-judge.prompt.md` — run it in Copilot chat (pick a higher model
tier for judging if you like). Hybrid mode: point it at `eval_report.json`,
it writes `judge_verdicts.json`, merge with
`eval_rag.py --golden <set> --report eval_report.json --merge-verdicts
judge_verdicts.json`. Standalone mode: the prompt can drive the whole
benchmark itself.

## Maintainers: publishing (gated)

Every release goes through `.github/workflows/publish-pypi.yml`:

1. Bump `version` in `mcp/<pkg>/pyproject.toml` on a PR and merge to main.
2. Tag `<pkg>-v<X.Y.Z>rc1` and push. The workflow validates the tag
   (version match, commit on main, version newer than PyPI), builds the
   rc wheel, runs unit tests + e2e on it across 3 OSes, publishes the
   pre-release (pip ignores pre-releases — users can't get it), then
   verifies it by installing **from PyPI** on all 3 OSes.
3. Wait for that run to go fully green, then tag `<pkg>-v<X.Y.Z>` on the
   **same commit** and push. The final build is refused unless a green RC
   run exists for the same SHA — so what users get is exactly what was
   verified end to end.

Order matters: publish `confluence-knowledge` first. local/SP pin the
engine with `~=`, which only accepts released finals — their validate
step fails until the engine final is on PyPI.

There is no bypass — a hotfix goes through the same rc → final flow.

### If a release turns out bad

**Yank it** (immediate): PyPI → project → Manage → Releases → `<version>`
→ Options → Yank, with a reason. Effects: `pip install -U` skips it, the
services' own `update` never targets it, and doctor flags it to everyone
already on it so they move off. Then fix forward — new patch through the
rc → final flow.

If a FINAL release's post-publish verification fails, the workflow opens
a tracking issue with this runbook automatically.
## Human feedback loop

- `submit_feedback(scope, question, correction, note)` — stores a saved
correction (embedded + similarity-matched).
- `search` auto-returns `user_corrections` when a stored correction
  matches the question at cosine ≥ 0.75 (`*_FEEDBACK_MIN_SIM` to tune) —
  the agent discloses it alongside the docs, never overrides.
- `feedback_report(scope)` — aggregates corrections → the agent can
  suggest which pages look outdated/confusing.
- Stored at `<index_root>/<scope>.feedback.db` (SQLite, WAL — survives concurrent writers); `clean` wipes it.
