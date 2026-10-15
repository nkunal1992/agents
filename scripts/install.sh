#!/usr/bin/env bash
# Install reusable Copilot agents into a repo's .github/agents/ directory.
#
# Usage:
#   install.sh [target-repo]     # defaults to current directory
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS_SRC="$SCRIPT_DIR/../agents"
TARGET_REPO="${1:-.}"
TARGET_DIR="$TARGET_REPO/.github/agents"

if [[ ! -d "$TARGET_REPO/.git" ]]; then
  echo "error: $TARGET_REPO is not a git repository" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR"

copied=0
skipped=0
for f in "$AGENTS_SRC"/*.agent.md; do
  name="$(basename "$f")"
  if [[ -f "$TARGET_DIR/$name" ]]; then
    echo "skip  $name (already exists)"
    skipped=$((skipped + 1))
  else
    cp "$f" "$TARGET_DIR/$name"
    echo "copy  $name"
    copied=$((copied + 1))
  fi
done

echo
echo "Installed $copied agent(s) into $TARGET_DIR ($skipped skipped)."
echo "Commit them so they're available in the repo: git add .github/agents && git commit"
