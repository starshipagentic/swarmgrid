#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

# ── Health check: is everything ready to run? ──────────────────

problems=()

[[ ! -f "board-routes.yaml" ]] && problems+=("no board-routes.yaml")
[[ ! -f "operator-settings.yaml" ]] && problems+=("no operator-settings.yaml")
[[ ! -f "$HOME/.atlassian-token" ]] && problems+=("no Jira API token")
command -v tmux &>/dev/null || problems+=("tmux not installed")
[[ ! -d ".venv" ]] && problems+=("no Python venv")

WORKDIR=$(grep "working_dir:" operator-settings.yaml 2>/dev/null | head -1 | sed 's/.*working_dir: *//' | tr -d "'\"")
if [[ -n "$WORKDIR" && ! -d "$WORKDIR" ]]; then
  echo "⚠ Working directory does not exist: $WORKDIR"
  echo "  Fix it in Setup page after launch (http://127.0.0.1:8787/setup)"
  echo ""
fi

if [[ ${#problems[@]} -gt 0 ]]; then
  for p in "${problems[@]}"; do echo "✗ $p"; done
  echo ""
  echo "→ Running setup to fix..."
  echo ""
  exec "$ROOT_DIR/setup.sh"
fi

# ── Fast track: everything good, just start ────────────────────

source .venv/bin/activate
pip install -e . -q 2>&1 | tail -1

echo "→ Starting swarmgrid on http://127.0.0.1:8787/board"
exec swarmgrid web --config board-routes.yaml --host 127.0.0.1 --port 8787
