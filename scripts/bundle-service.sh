#!/usr/bin/env bash
# Assemble a self-contained <name>-knowledge bundle for AI-store upload.
#
# Output: dist/<name>-knowledge-bundle/ (+ .zip with --zip)
#
# Layout:
#   <name>-knowledge.agent.md    → .github/agents/
#   prompts/*.prompt.md          → .github/prompts/ (optional)
#   service/                     → vendored MCP service source
#   <pkg>-<ver>-py3-none-any.whl → prebuilt wheel (no build backend needed)
#   mcp.json.example             → IDE config template
#   README.md / EXPLAINED.md / SLIDES.md (when present)
#
# Usage: scripts/bundle-service.sh <confluence|local|sharepoint> [--zip]
#        [--include-wheels <wheelhouse>]
set -euo pipefail

NAME="${1:?usage: bundle-service.sh <name> [--zip]}"
shift || true

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG_DIR="$ROOT/mcp/${NAME}-knowledge"
PKG_US="${NAME}_knowledge"          # python package name
OUT="$ROOT/dist/${NAME}-knowledge-bundle"
ZIP=0
WHEELS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --zip) ZIP=1; shift ;;
    --include-wheels) WHEELS="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

# per-agent settings
case "$NAME" in
  confluence)
    SERVER_MODULE="confluence_knowledge.server"
    ENV_VAR="CONF_KNOWLEDGE_WORKDIR"
    PROMPTS=(confluence-ask confluence-refresh)
    README_SRC="bundle/README.md"
    ;;
  local)
    SERVER_MODULE="local_knowledge.server_local"
    ENV_VAR="LOCAL_KNOWLEDGE_WORKDIR"
    PROMPTS=(local-ask local-refresh)
    README_SRC="bundle/local-README.md"
    ;;
  sharepoint)
    SERVER_MODULE="sharepoint_knowledge.server_sp"
    ENV_VAR="SP_KNOWLEDGE_WORKDIR"
    PROMPTS=(sharepoint-ask sharepoint-refresh)
    README_SRC="bundle/sharepoint-README.md"
    ;;
  *)
    echo "no bundler config for '$NAME' — add a case block" >&2; exit 1 ;;
esac

rm -rf "$OUT"
mkdir -p "$OUT/prompts" "$OUT/service"

# agent + prompts
cp "$ROOT/agents/${NAME}-knowledge.agent.md" "$OUT/"
for p in "${PROMPTS[@]}"; do
  cp "$ROOT/prompts/${p}.prompt.md" "$OUT/prompts/"
done

# vendored service source (exclude caches/build artifacts)
( cd "$PKG_DIR" && \
  find . -type f \
    ! -path './.venv/*' ! -path '*/__pycache__/*' ! -name '*.pyc' \
    ! -path './dist/*' ! -path './*.egg-info/*' ! -path './.pytest_cache/*' \
    ! -path './.ruff_cache/*' ! -path './build/*' ! -path './work/*' \
    ! -name '.DS_Store' \
    -print ) | ( cd "$PKG_DIR" && tar -cf - -T - ) | \
    tar -xf - -C "$OUT/service"

# IDE config template
cat > "$OUT/mcp.json.example" <<JSON
{
  "servers": {
    "${NAME}-knowledge": {
      "command": "~/.${NAME}-knowledge/venv/bin/python",
      "args": ["-m", "${SERVER_MODULE}"],
      "env": { "${ENV_VAR}": "<absolute path to your repo>" }
    }
  }
}
JSON

# prebuilt wheel — lets `pip install <whl>[models]` work without any build
# backend or setuptools download (offline/corporate-friendly)
python3 -c "import build" 2>/dev/null || python3 -m pip install --quiet --user build
( cd "$PKG_DIR" && python3 -m build --wheel --outdir "$OUT" >/dev/null )
echo "  wheel: $(ls "$OUT"/*.whl | xargs -n1 basename)"

# human-facing README (what teams see on the store listing + in the zip)
cp "$ROOT/$README_SRC" "$OUT/README.md"
# explainer + slides — present for confluence, optional for others
[ -f "$ROOT/docs/${NAME}-knowledge-explained.md" ] && \
  cp "$ROOT/docs/${NAME}-knowledge-explained.md" "$OUT/EXPLAINED.md"
[ -f "$ROOT/docs/${NAME}-knowledge-slides.md" ] && \
  cp "$ROOT/docs/${NAME}-knowledge-slides.md" "$OUT/SLIDES.md"

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
  ( cd "$ROOT/dist" && rm -f "${NAME}-knowledge-bundle.zip" && \
    zip -rq "${NAME}-knowledge-bundle.zip" "${NAME}-knowledge-bundle" )
  echo "dist/${NAME}-knowledge-bundle.zip"
else
  echo "dist/${NAME}-knowledge-bundle/"
fi
