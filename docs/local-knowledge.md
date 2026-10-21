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
`crawl_step` returning `state: abandoned` means the job was superseded or
fenced — it can never finish; call `start_crawl` for a fresh job.

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
| `KNOWLEDGE_THREADS` | native inference threads per session (default ≈ cores/4, max 4) |
| `KNOWLEDGE_BATCH_TOKENS` / `KNOWLEDGE_MAX_BATCH` | inference micro-batch bounds — attention cost (items × longest²) 2.5 M / 4 items |
| `KNOWLEDGE_IDLE_UNLOAD_S` | unload models after this idle time (600; 0 = never) |
| `KNOWLEDGE_NO_NICE` | `1` disables the lowered OS scheduling priority |
| `KNOWLEDGE_MIN_FREE_MB` | pause embedding while free memory < this floor (1500; 0 = off) |
| `KNOWLEDGE_MEM_PAUSE_MAX_S` / `KNOWLEDGE_MEM_PAUSE_COOLDOWN_S` | low-memory pause ceiling — after 600 s paused the crawl resumes for 60 s of work before the guard may re-engage (phase shows `resuming: memory floor unreachable`); a miscalibrated floor can never deadlock |
| `LOCAL_KNOWLEDGE_AUTO_REINDEX` | `0` disables the automatic rebuild a stale-model index triggers on `connect` (default on — user is notified once) |
| `KNOWLEDGE_FETCH_CONCURRENCY` / `KNOWLEDGE_FETCH_MAX` | fetch workers (6) / adaptive ceiling — default 0 caps the window at the base; the window halves under CPU ≥75 % and bottoms at 1 over ≥90 % (`fetch_cap` in progress) |
| `KNOWLEDGE_BATTERY_SAVER` | `0` disables on-battery throttling (fetch concurrency halves, ~50% embed duty cycle) |
| `KNOWLEDGE_NOTIFY` | `0` disables the desktop notification on crawl done/error/cancelled (default on) |

## Resource usage

Models load lazily on first `connect` and unload after ~10 idle minutes —
an idle server uses ~0.2 GB RSS, an active one ~1.9 GB. Every IDE window
runs its own server; `doctor`'s `resources` flag warns when more than 4
are running. Inference is micro-batched and threads/priority are capped
so the laptop stays responsive while indexing.

Tools run on worker threads (a slow call can't block the MCP loop), and a
cross-process lease gives each job exactly one worker — a second IDE
window polls the same crawl instead of racing it. `cancel_crawl` stops a
running crawl within seconds (`cancelled` is terminal — it never
auto-resumes) and keeps the previously published snapshot. `wait_for_crawl`
long-polls ≤120s and returns the moment progress moves (`phase`,
`pages_done`, `rate_ppm`, `eta_s`); `connect`/`status` carry `last_crawl`,
and a desktop notification fires on done/error/cancelled.

Files and directory listings are fetched on `KNOWLEDGE_FETCH_CONCURRENCY`
worker threads (default 6) while extraction/embedding stays on the crawl
thread. Re-crawls skip re-embedding unchanged files (`content_hash`
match) — a re-scan of a big tree mostly re-reads bytes and reuses rows.

## Updating safely

`local-knowledge update` is deliberately conservative:

- **Only soaked releases** — a release must sit on PyPI for 3 days before
  it's offered (`local-knowledge update --latest` skips the soak; `--to <ver>` pins
  exactly). A just-published version shows as "soaking until <date>" in
  doctor — that's the intended delay, not a bug.
- **Self-test after install** — it runs `import server → tool list →
  health checks → a real mini-RAG` on the new code before declaring
  success.
- **Automatic rollback** — a `pip freeze` snapshot goes to
  `<home>/rollback/`; any stage failing restores the previous environment
  and re-verifies it. `local-knowledge update --rollback` restores the last
  snapshot manually. The last 3 snapshots are kept.
- **Yanked releases** — never installable. If you're on one, doctor shows
  a `release` flag and `update` moves you to the last-good release even
  when that's a downgrade.
- **Quit the IDE first** — a running MCP server holds the old code open.
  On Windows a running exe can't replace itself, so `update` prints the
  equivalent `venv-python -m <cli_module> update` command to run instead.
- `local-knowledge selftest` — the same checks on demand ("is my install actually
  working?"), exit 0/1.

Env knobs: `KNOWLEDGE_UPDATE_INDEX` (mirror URL template),
`KNOWLEDGE_UPDATE_SOAK_DAYS` (default 3 — 0 disables the delay).

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
| `deps` | an install was interrupted — a dep isn't importable | the pinned reinstall doctor prints — `pip install "local-knowledge[models]==<installed>"` |
| `model_embed / model_rerank / model_rerank_light / model_sparse` | partial model download (dir exists, locked file missing) | `prepare-models` |
| `registration` | no IDE config contains the server | `register --ide auto` |
| `index:<scope>` | `corrupt` → delete index, re-crawl; `older model` → one `start_crawl` rebuild | `clean` / `start_crawl` |
| `creds_valid` | token rejected or missing (401) | `setup` again |
| `disk_free` | < 2 GB free — models won't fit | free space |
| `python` | venv interpreter < 3.11 | rebuild venv on 3.11+ |
| `proxy` | informational — shows HTTPS_PROXY/HTTP_PROXY visibility | — |
| `resources` | shows the resource limits + running server count; red when >4 knowledge servers run | close unused IDE windows (idle servers unload models anyway) |

`doctor` exits non-zero while any flag is red.

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

## Human feedback loop

- `submit_feedback(scope, question, correction, note)` — stores a saved
correction (embedded + similarity-matched).
- `search` auto-returns `user_corrections` when a stored correction
  matches the question at cosine ≥ 0.75 (`*_FEEDBACK_MIN_SIM` to tune) —
  the agent discloses it alongside the docs, never overrides.
- `feedback_report(scope)` — aggregates corrections → the agent can
  suggest which pages look outdated/confusing.
- Stored at `<index_root>/<scope>.feedback.db` (SQLite, WAL — survives concurrent writers); `clean` wipes it.
