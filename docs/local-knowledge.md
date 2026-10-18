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
path, in that order), downloads the local models (~1.4 GB once), writes the IDE MCP
config, and asks for one reload.

Manual install (same commands the agent runs):

```bash
python3 -m venv ~/.local-knowledge/venv
~/.local-knowledge/venv/bin/python -m pip install "local-knowledge[models]"
~/.local-knowledge/venv/bin/local-knowledge prepare-models   # ~1.4 GB, once
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

## Retrieval limits (by design)

- Evidence text returned by `search` is capped at 12000 chars per chunk;
  `truncated: true` flags it — `get_evidence` returns the full text.
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

## Human feedback loop

- `submit_feedback(scope, question, correction, note)` — stores a saved
correction (embedded + similarity-matched).
- `search` auto-returns `user_corrections` when a stored correction
  matches the question at cosine ≥ 0.75 (`*_FEEDBACK_MIN_SIM` to tune) —
  the agent discloses it alongside the docs, never overrides.
- `feedback_report(scope)` — aggregates corrections → the agent can
  suggest which pages look outdated/confusing.
- Stored at `<index_root>/<scope>.feedback.json`; `clean` wipes it.
