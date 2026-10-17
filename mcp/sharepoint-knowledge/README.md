# sharepoint-knowledge

SharePoint connector on the shared grounded-Q&A engine — Site Pages +
document libraries (docx/pdf/pptx/xlsx/csv/txt/md/html), Entra device-code
sign-in, local models, cited answers. Same engine as
[`confluence-knowledge`](../confluence-knowledge) and
[`local-knowledge`](../local-knowledge).

- `sharepoint_knowledge/auth_sp.py` — MSAL device-code flow, token cache in
  the OS keyring (protected-file fallback), silent refresh
- `sharepoint_knowledge/client_sp.py` — GET-only Graph client; downloads go
  through `@microsoft.graph.downloadUrl` WITHOUT the Bearer header
- `sharepoint_knowledge/crawl_sp.py` — durable BFS site → pages + libraries
- `sharepoint_knowledge/server_sp.py` — the `sharepoint-knowledge/*` tools
- `sharepoint_knowledge/cli_sp.py` — setup/doctor/prepare-models/register/
  update/serve/clean

See `docs/sharepoint-knowledge.md` for the full guide.
