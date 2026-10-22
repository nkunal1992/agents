# confluence-knowledge (local MCP service)

Local companion service for the `confluence-knowledge` Copilot agent.
See `../../docs/confluence-knowledge.md` for the full guide.

- GET-only `ConfluenceClient` — non-GET raises `SecurityException` pre-flight.
- Recursive subtree crawl (Cloud v2 + Data Center), resumable.
- Local ONNX embeddings (`nomic-embed-text-v1.5`, quantized) + sparse leg (`Qdrant/bm25`)
  + reranker (`mxbai-rerank-xsmall-v1`, quantized) — MIT/Apache-2.0, pinned in
  `models.lock.json`, no torch, never a cloud embedding call.
- SQLite + FTS5 + vector-blob index under `.conf_knowledge_index/`.
- Credentials via OS keyring / env var / chmod-600 `.conf_auth.json`.

## Dev

```bash
python3.11+ -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # all offline — no network needed
.venv/bin/python -m ruff check src tests
```

Before every release, run the end-to-end smoke test. It spawns the real stdio
server through the MCP client SDK and uses the real models against a fake
Confluence on localhost, with simulated network latency:

```bash
.venv/bin/pip install -e '.[models]'
CONF_KNOWLEDGE_HOME=/tmp/ck/home .venv/bin/confluence-knowledge prepare-models
CONF_KNOWLEDGE_HOME=/tmp/ck/home .venv/bin/python scripts/e2e_smoke.py   # ends with "E2E OK"
```

`e2e_bootstrap.py` goes further — it replays the agent's entire install flow
in a sandboxed $HOME (python discovery → venv → `pip install` from the
shipped `dist/` bundle → models → credentials → `register` → the real server
over stdio → a full Q&A round-trip). Run it before every release:

```bash
CK_MODEL_CACHE=/tmp/ck/home/models .venv/bin/python scripts/e2e_bootstrap.py  # "E2E BOOTSTRAP OK"
```

## CLI

| Command | Purpose |
|---|---|
| `confluence-knowledge setup` | guided env/site/token setup (hidden input, keyring) |
| `confluence-knowledge prepare-models` | fetch pinned local models once |
| `confluence-knowledge doctor` | verify config + heartbeat + models |
| `confluence-knowledge serve` | run the stdio MCP server (what the IDE launches) |
| `confluence-knowledge clean` | delete the local index (`~/.confluence-knowledge/work` unless `CONF_KNOWLEDGE_WORKDIR` is set) |
