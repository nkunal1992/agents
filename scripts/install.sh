#!/usr/bin/env bash
# Install reusable Copilot agents (and their prompt files) into a repo's .github/ directory.
#
# Usage:
#   install.sh [--only <agent>]... [target-repo]     # target defaults to current directory
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS_SRC="$SCRIPT_DIR/../agents"
PROMPTS_SRC="$SCRIPT_DIR/../prompts"

usage() {
  cat <<'EOF'
Usage: install.sh [--only <agent>]... [target-repo]

Copies agents into <target-repo>/.github/agents/ and their prompt files into
<target-repo>/.github/prompts/. Existing files are never overwritten.

Options:
  --only <agent>   Install only this agent and its prompt files (repeatable)
  -h, --help       Show this help

Examples:
  install.sh                                         # everything, into the current repo
  install.sh --only cve-remediator ~/work/my-service
  install.sh --only cve-remediator --only code-reviewer ~/work/my-service
EOF
}

available_agents() {
  for f in "$AGENTS_SRC"/*.agent.md; do basename "$f" .agent.md; done | paste -sd ' ' -
}

ONLY=""
TARGET_REPO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --only)
      if [[ $# -lt 2 ]]; then
        echo "error: --only requires an agent name" >&2
        exit 1
      fi
      name="${2%.agent.md}"
      if [[ ! -f "$AGENTS_SRC/$name.agent.md" ]]; then
        echo "error: unknown agent '$name'. Available: $(available_agents)" >&2
        exit 1
      fi
      ONLY="$ONLY $name"
      shift 2
      ;;
    -*)
      echo "error: unknown option '$1'" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ -n "$TARGET_REPO" ]]; then
        echo "error: only one target repo may be given" >&2
        exit 1
      fi
      TARGET_REPO="$1"
      shift
      ;;
  esac
done
TARGET_REPO="${TARGET_REPO:-.}"
AGENTS_DIR="$TARGET_REPO/.github/agents"
PROMPTS_DIR="$TARGET_REPO/.github/prompts"

if [[ ! -d "$TARGET_REPO/.git" ]]; then
  echo "error: $TARGET_REPO is not a git repository" >&2
  exit 1
fi

selected() {
  [[ -z "$ONLY" ]] && return 0
  case " $ONLY " in *" $1 "*) return 0 ;; esac
  return 1
}

copied=0
skipped=0
install_file() {
  local src="$1" dest_dir="$2" name
  name="$(basename "$src")"
  mkdir -p "$dest_dir"
  if [[ -f "$dest_dir/$name" ]]; then
    echo "skip  $name (already exists)"
    skipped=$((skipped + 1))
  else
    cp "$src" "$dest_dir/$name"
    echo "copy  $name"
    copied=$((copied + 1))
  fi
}

for f in "$AGENTS_SRC"/*.agent.md; do
  selected "$(basename "$f" .agent.md)" && install_file "$f" "$AGENTS_DIR"
done

for p in "$PROMPTS_SRC"/*.prompt.md; do
  [[ -f "$p" ]] || continue
  agent="$(sed -n 's/^agent:[[:space:]]*//p' "$p" | head -n 1 | tr -d "\"' ")"
  selected "$agent" && install_file "$p" "$PROMPTS_DIR"
done

# Warn when an installed agent hands off to an agent that isn't installed.
for f in "$AGENTS_SRC"/*.agent.md; do
  name="$(basename "$f" .agent.md)"
  selected "$name" || continue
  for target in $(sed -n '/^handoffs:/,/^---/s/^[[:space:]]*agent:[[:space:]]*//p' "$f" | tr -d "\"'"); do
    if [[ ! -f "$AGENTS_DIR/$target.agent.md" ]]; then
      echo "warn  $name hands off to '$target', which is not installed — its handoff button won't work."
      echo "      Add it with: install.sh --only $target $TARGET_REPO"
    fi
  done
done

echo
echo "Installed $copied file(s) into $TARGET_REPO/.github ($skipped skipped)."
echo "Commit them so they're available in the repo: git add .github && git commit"
