#!/usr/bin/env bash
# Assemble a self-contained confluence-knowledge bundle for AI-store upload.
#
# Output: dist/confluence-knowledge-bundle/ (+ .zip with --zip)
#
# Layout:
#   confluence-knowledge.agent.md   → .github/agents/
#   prompts/*.prompt.md             → .github/prompts/ (optional)
#   service/                        → vendored MCP service source (pip install ./service[models])
#   mcp.json.example                → IDE config template
#   README.md                       → install notes
#
# Usage: scripts/bundle-confluence.sh [--zip] [--include-wheels <wheelhouse>]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/dist/confluence-knowledge-bundle"
ZIP=0
WHEELS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --zip) ZIP=1; shift ;;
    --include-wheels) WHEELS="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

rm -rf "$OUT"
mkdir -p "$OUT/prompts" "$OUT/service"

# agent + prompts
cp "$ROOT/agents/confluence-knowledge.agent.md" "$OUT/"
cp "$ROOT/prompts/confluence-ask.prompt.md" "$ROOT/prompts/confluence-refresh.prompt.md" "$OUT/prompts/"

# vendored service source (exclude caches/build artifacts)
( cd "$ROOT/mcp/confluence-knowledge" && \
  find . -type f \
    ! -path './.venv/*' ! -path '*/__pycache__/*' ! -name '*.pyc' \
    ! -path './dist/*' ! -path './*.egg-info/*' ! -path './.pytest_cache/*' \
    ! -path './.ruff_cache/*' ! -path './.conf_knowledge_index/*' \
    ! -name '.DS_Store' \
    -print ) | ( cd "$ROOT/mcp/confluence-knowledge" && tar -cf - -T - ) | \
    tar -xf - -C "$OUT/service"

# IDE config template
cat > "$OUT/mcp.json.example" <<'JSON'
{
  "servers": {
    "confluence-knowledge": {
      "command": "~/.confluence-knowledge/venv/bin/python",
      "args": ["-m", "confluence_knowledge.server"],
      "env": { "CONF_KNOWLEDGE_WORKDIR": "<absolute path to your repo>" }
    }
  }
}
JSON

# human-facing install notes
cat > "$OUT/README.md" <<'EOF'
# confluence-knowledge bundle

1. Copy `confluence-knowledge.agent.md` into your repo at `.github/agents/`
   (or upload to the agent store). Prompts in `prompts/` are optional —
   they go to `.github/prompts/`.
2. Keep the `service/` folder somewhere in the repo (e.g. repo root or
   `tools/confluence-knowledge-service/`). The agent auto-detects it on
   first run and installs it locally — no GitHub access needed for the code.
3. First run: the agent installs the service, downloads the two Apache-2.0
   models (Hugging Face, or your internal mirror), collects your token via
   hidden input in your own terminal, writes `.vscode/mcp.json` (see
   `mcp.json.example`), and asks you to reload once.
4. Offline model install: drop `all-MiniLM-L6-v2/` and
   `ms-marco-MiniLM-L6-v2/` into `~/.confluence-knowledge/models/`.

Requires: Python ≥3.11. Dependencies resolve from PyPI (or your internal
index — set `PIP_INDEX_URL` / use `wheels/` if provided).
EOF

# optional vendored wheels for fully-offline pip installs
if [[ -n "$WHEELS" ]]; then
  mkdir -p "$OUT/wheels"
  cp "$WHEELS"/*.whl "$OUT/wheels/" 2>/dev/null || true
  cat >> "$OUT/README.md" <<'EOF'

## Offline deps (wheels/ included)
The agent's pip install will still look for deps on PyPI; to use the bundled
wheels: `pip install --no-index --find-links wheels "service[models]"`.
EOF
fi

if [[ "$ZIP" -eq 1 ]]; then
  ( cd "$ROOT/dist" && rm -f confluence-knowledge-bundle.zip && \
    zip -rq confluence-knowledge-bundle.zip confluence-knowledge-bundle )
  echo "dist/confluence-knowledge-bundle.zip"
else
  echo "dist/confluence-knowledge-bundle/"
fi
