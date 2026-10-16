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

# human-facing README (what teams see on the store listing + in the zip)
cp "$ROOT/bundle/README.md" "$OUT/README.md"
# plain-language explainer — for whoever presents/defends the agent internally
cp "$ROOT/docs/confluence-knowledge-explained.md" "$OUT/EXPLAINED.md"
# slide deck (Marp) for presenting the agent internally
cp "$ROOT/docs/confluence-knowledge-slides.md" "$OUT/SLIDES.md"

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
