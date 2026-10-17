# local-knowledge

Local file/folder indexer + grounded-Q&A MCP service — the same engine as
[`confluence-knowledge`](../confluence-knowledge), pointed at the filesystem
instead of Confluence. No credentials, no HTTP.

- `local_knowledge/crawl_local.py` — durable filesystem BFS (secret-file
  exclusions, symlink-escape guards, checkpointed resume)
- `local_knowledge/server_local.py` — the `local-knowledge/*` MCP tools
- `local_knowledge/cli_local.py` — `local-knowledge` CLI (no `setup`; there's
  nothing to authenticate)

Shares `confluence_knowledge`'s index, extraction (`extract` +
`extract_files`), retrieval, registration and auth plumbing.

See `docs/local-knowledge.md` in the repo root for the full guide and
`bundle/local-README.md` for the store-listing copy.
