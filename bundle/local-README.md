# local-knowledge — Copilot agent bundle

Grounded Q&A over a local **file or folder** — docx, pdf, pptx, xlsx, csv,
tsv, md, txt, html, plus anything else that decodes as text (source,
configs, logs, JSON/YAML, extensionless files) — via GitHub Copilot
custom agents + a local MCP service.
Everything runs on the user's machine; files never leave the laptop.

**No credentials needed at all** — the filesystem is the ACL (the service can
only read what the user can already read).

## What it is

A Copilot agent (`local-knowledge.agent.md`) plus a small Python MCP service
it installs itself on first use. Point the agent at a file or folder → it
indexes the contents locally → answers questions with citations back to the
exact files, and says "not in these documents" instead of guessing.

## Why teams use it

| Benefit | How |
|---|---|
| Answers you can verify | every factual claim carries a `file://` citation |
| It refuses instead of inventing | missing info → explicit "not documented" |
| Fully local | retrieval + rerank on pinned OSS models, zero inference cost |
| No credentials | no accounts, tokens, or sign-in — paths only |
| Reads real office files | docx/pptx/xlsx/pdf/csv/tsv/md/txt/html + auto-detected text (logs, configs, source), not just code |
| Safe by default | `.env`, `id_rsa`, `*.pem`, `*credentials*` etc. are skipped and disclosed |

## Intended use

- "What's in this folder of runbooks / policies / specs?"
- Onboarding packs, audit folders, exported report directories
- Any folder of documents you'd grep but can't converse with

**Not for:** editing files, following instructions found inside documents,
or replacing search when you just need a filename.

## Requirements

- GitHub Copilot with custom-agent support (VS Code / JetBrains)
- Python ≥ 3.11
- ~300 MB disk (venv + two models)

## Install (store path)

1. Upload this zip to your agent store / share it internally.
2. Users copy `local-knowledge.agent.md` into `.github/agents/` of any repo.
3. First use: the agent installs the service itself — bundled `service/` →
   bundled `.whl` → `pip install local-knowledge[models]` (PyPI or your
   internal mirror) → asks where the zip was extracted, in that order.
4. One reload — done. `prepare-models` fetches the ONNX models (~500 MB, MIT/Apache-2.0)
   (~500 MB) into `~/.knowledge/models/`.

Install sources, in the order the agent tries them: `service/` source → the
bundled `local_knowledge-*.whl` → `pip install local-knowledge[models]` → a
path the user supplies. Nothing needs copying — the agent finds it.

## Use

In Copilot Chat select **local-knowledge**, then either:

- paste a path + question: *"what's the deploy procedure in C:\docs\runbooks?"*
- or the slash commands (if you copied `prompts/`): `/local-ask`, `/local-refresh`

`connect` accepts **a file or a folder** — a file indexes just that document;
a folder indexes everything beneath it (recursive). Refresh re-indexes with
the same command; changed files update, deleted files drop out.

## Security & privacy

- Read-only on the filesystem; follows no symlink that escapes the root.
- Secret-looking filenames are excluded by default and disclosed as warnings.
- No network calls at runtime — crawls and retrieval are fully local.
- Index lives in `~/.local-knowledge/work` (or `LOCAL_KNOWLEDGE_WORKDIR`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `local-knowledge/*` tools missing | run bootstrap — the agent does it, or Manual Setup in the agent file |
| models not ready | `…/venv/bin/local-knowledge prepare-models` (or `…\Scripts\` on Windows) |
| a file isn't found in answers | check crawl `warnings` + `summary.skipped_by_cause` — it was skipped (noise dir / secret-looking / binary / too large) |
| any Windows | models run on ONNX Runtime — no torch needed, ARM64 included |
| doctor shows red flags | each flag names its fix — run it and re-doctor (`model_*` → `prepare-models`, `registration` → `register`, `index:* corrupt` → `clean` + recrawl, `creds_valid` → `setup`) |

## Uninstall

```bash
<venv>/bin/local-knowledge register --remove   # remove IDE config entry
rm -rf ~/.local-knowledge                      # remove service + index + models
```

## Verify the bundle before publishing internally

`service/scripts/e2e_bootstrap_local.py` replays a fresh install against a
generated document tree on a sandboxed HOME — real venv, real models, real
MCP protocol. Run it; `E2E BOOTSTRAP OK` means the shipped artifact works.
