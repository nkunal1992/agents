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

Scopes are bound to the **tenant + signed-in account + root** —
per-account indexes are a security boundary: a different sign-in sees
an empty cache (`cached: false`) and re-crawls, so one account's
content can never leak into another's answers. (Upgrading from
≤0.2.x rebinds scopes — the first crawl re-indexes; expected.)

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
- `crawl_step` returning `state: abandoned` means the job was
  superseded or fenced — it can never finish; call `start_crawl`
  for a fresh job

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
| `KNOWLEDGE_THREADS` | native inference threads per session (default ≈ cores/4, max 4) |
| `KNOWLEDGE_BATCH_TOKENS` / `KNOWLEDGE_MAX_BATCH` | inference micro-batch bounds — attention cost (items × longest²) 2.5 M / 4 items |
| `KNOWLEDGE_IDLE_UNLOAD_S` | unload models after this idle time (600; 0 = never) |
| `KNOWLEDGE_NO_NICE` | `1` disables the lowered OS scheduling priority |
| `KNOWLEDGE_MIN_FREE_MB` | pause embedding while free memory < this floor (1500; 0 = off) |
| `KNOWLEDGE_MEM_PAUSE_MAX_S` / `KNOWLEDGE_MEM_PAUSE_COOLDOWN_S` | low-memory pause ceiling — after 600 s paused the crawl resumes for 60 s of work before the guard may re-engage (phase shows `resuming: memory floor unreachable`); a miscalibrated floor can never deadlock |
| `SP_KNOWLEDGE_AUTO_REINDEX` | `0` disables the automatic rebuild a stale-model index triggers on `connect` (default on — user is notified once) |
| `KNOWLEDGE_FETCH_CONCURRENCY` / `KNOWLEDGE_FETCH_MAX` | fetch workers (6) / adaptive ceiling — default 0 caps the window at the base; the window halves under CPU ≥75 % and bottoms at 1 over ≥90 % (`fetch_cap` in progress) |
| `KNOWLEDGE_BATTERY_SAVER` | `0` disables on-battery throttling (fetch concurrency halves, ~50% embed duty cycle) |
| `KNOWLEDGE_NOTIFY` | `0` disables the desktop notification on crawl done/error/cancelled (default on) |

### Three-tier reporting

Running jobs surface live reporting fields (`status`, `crawl_step`,
`wait_for_crawl`):

- `pulse` — one-line status: `embedding | 7813/~9901 pages | 244 ppm |
  1656 MB RSS`
- `stages` — structured `fetch`/`embed` counts, `pct_complete`, `eta_s`
- terminal jobs carry `summary` — `errors_by_cause` grouped (access
  denied / server error / timeout / unreadable), `publish`
  (staged/deleted/kept), `index_size_mb`, `pages_indexed`


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

Page listings, item metadata, and downloads run on
`KNOWLEDGE_FETCH_CONCURRENCY` worker threads (default 6) — a large site
or document library crawls several times faster than sequential. Re-crawls
skip work aggressively: an item whose `lastModifiedDateTime` is unchanged
is not even downloaded, and any page whose content is unchanged reuses
its live rows instead of re-embedding.
| `SP_KNOWLEDGE_AUTH_BASE` | Entra authority override (e2e fake) |

## Roadmap

- SharePoint Server (on-prem, NTLM/auth-cookie) — the crawl/engine is
  connector-agnostic; needs a second client + auth module
- App-only client-credentials fallback for tenants that block device flow

## Updating safely

`sharepoint-knowledge update` is deliberately conservative:

- **Only soaked releases** — a release must sit on PyPI for 3 days before
  it's offered (`sharepoint-knowledge update --latest` skips the soak; `--to <ver>` pins
  exactly). A just-published version shows as "soaking until <date>" in
  doctor — that's the intended delay, not a bug.
- **Self-test after install** — it runs `import server → tool list →
  health checks → a real mini-RAG` on the new code before declaring
  success.
- **Automatic rollback** — a `pip freeze` snapshot goes to
  `<home>/rollback/`; any stage failing restores the previous environment
  and re-verifies it. `sharepoint-knowledge update --rollback` restores the last
  snapshot manually. The last 3 snapshots are kept.
- **Yanked releases** — never installable. If you're on one, doctor shows
  a `release` flag and `update` moves you to the last-good release even
  when that's a downgrade.
- **Quit the IDE first** — a running MCP server holds the old code open.
  On Windows a running exe can't replace itself, so `update` prints the
  equivalent `venv-python -m <cli_module> update` command to run instead.
- `sharepoint-knowledge selftest` — the same checks on demand ("is my install actually
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
| `deps` | an install was interrupted — a dep isn't importable | the pinned reinstall doctor prints — `pip install "sharepoint-knowledge[models]==<installed>"` |
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
