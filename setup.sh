#!/usr/bin/env bash
set -euo pipefail

#
# Set up swarmgrid for a new developer.
#
# One path. Installs everything, asks minimal questions, validates auth,
# auto-configures the board, and starts the web UI.
#
# Idempotent: re-running shows current values, Enter keeps them.
#
# Usage:
#   ./scripts/setup-dev.sh                          # interactive
#   ./scripts/setup-dev.sh --relay "ssh://..."       # pre-set relay
#   ./scripts/setup-dev.sh --email me@co.com --token XXXX --board-url "https://..."
#

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

RELAY=""
EMAIL=""
TOKEN=""
BOARD_URL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --relay)     RELAY="$2"; shift 2 ;;
    --email)     EMAIL="$2"; shift 2 ;;
    --token)     TOKEN="$2"; shift 2 ;;
    --board-url) BOARD_URL="$2"; shift 2 ;;
    --help|-h)
      echo "Usage: ./scripts/setup-dev.sh [--relay SERVER] [--email EMAIL] [--token TOKEN] [--board-url URL]"
      exit 0
      ;;
    *) echo "Unknown flag: $1"; exit 1 ;;
  esac
done

info()  { echo "→ $*"; }
ok()    { echo "✓ $*"; }
warn()  { echo "⚠ $*"; }

# Mask a string: show first 4 and last 4 chars
mask() {
  local s="$1"
  local len=${#s}
  if [[ $len -le 8 ]]; then
    echo "****"
  else
    echo "${s:0:4}...${s: -4}"
  fi
}

echo ""
echo "  swarmgrid developer setup"
echo "  ========================="
echo ""

# ── Read existing config values (for idempotent re-runs) ────────

EXISTING_EMAIL=""
EXISTING_SITE_URL=""
EXISTING_PROJECT_KEY=""
EXISTING_BOARD_ID=""

if [[ -f "$ROOT_DIR/operator-settings.yaml" ]]; then
  EXISTING_EMAIL=$(grep "email:" "$ROOT_DIR/operator-settings.yaml" 2>/dev/null | head -1 | sed 's/.*email: *//' | tr -d "'\"" || true)
fi
if [[ -f "$ROOT_DIR/board-routes.yaml" ]]; then
  EXISTING_SITE_URL=$(grep "site_url:" "$ROOT_DIR/board-routes.yaml" 2>/dev/null | head -1 | sed 's/.*site_url: *//' | tr -d "'\"" || true)
  EXISTING_PROJECT_KEY=$(grep "project_key:" "$ROOT_DIR/board-routes.yaml" 2>/dev/null | head -1 | sed 's/.*project_key: *//' | tr -d "'\"" || true)
  EXISTING_BOARD_ID=$(grep "board_id:" "$ROOT_DIR/board-routes.yaml" 2>/dev/null | head -1 | sed 's/.*board_id: *//' | tr -d "'\"" || true)
fi

TOKEN_FILE="$HOME/.atlassian-token"
EXISTING_TOKEN=""
if [[ -f "$TOKEN_FILE" ]]; then
  EXISTING_TOKEN=$(cat "$TOKEN_FILE")
fi

# ── Step 1: Dependencies (all automatic, no questions) ──────────

# Python venv
if [[ ! -d ".venv" ]]; then
  info "Creating Python virtual environment..."
  python3 -m venv .venv
fi
source .venv/bin/activate
info "Installing swarmgrid..."
pip install -e . -q 2>&1 | tail -1
ok "Python + swarmgrid installed"

# tmux
if ! command -v tmux &>/dev/null; then
  if command -v brew &>/dev/null; then
    info "Installing tmux..."
    brew install tmux 2>&1 | tail -3
    ok "tmux installed"
  else
    echo "✗ tmux is required. Install it:"
    echo "  macOS:  brew install tmux"
    echo "  Ubuntu: sudo apt install tmux"
    exit 1
  fi
else
  ok "tmux found"
fi

# upterm (auto-install, no question)
if ! command -v upterm &>/dev/null; then
  if command -v brew &>/dev/null; then
    info "Installing upterm (session sharing)..."
    brew install upterm 2>&1 | tail -3
    ok "upterm installed"
  else
    warn "upterm not found — session sharing won't work until you install it"
    warn "  See: https://github.com/owenthereal/upterm/releases"
  fi
else
  ok "upterm found"
fi

# SSH key
if ! ls ~/.ssh/id_*.pub &>/dev/null 2>&1; then
  info "Generating SSH key (needed for session sharing)..."
  ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -C "$(whoami)@$(hostname -s)"
  ok "SSH key created"
else
  ok "SSH key found"
fi

echo ""

# ── Step 2: Your Jira credentials ───────────────────────────────

if [[ -z "$EMAIL" ]]; then
  if [[ -n "$EXISTING_EMAIL" ]]; then
    read -rp "→ Your Jira email [$EXISTING_EMAIL]: " EMAIL
    [[ -z "$EMAIL" ]] && EMAIL="$EXISTING_EMAIL"
  else
    read -rp "→ Your Jira email (the one you log into Atlassian with): " EMAIL
  fi
fi
[[ -z "$EMAIL" ]] && { echo "✗ Email is required."; exit 1; }

if [[ -n "$TOKEN" ]]; then
  echo "$TOKEN" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  ok "Token saved"
elif [[ -n "$EXISTING_TOKEN" ]]; then
  MASKED=$(mask "$EXISTING_TOKEN")
  read -rp "→ Jira API token [$MASKED] (Enter to keep): " NEW_TOKEN
  if [[ -n "$NEW_TOKEN" ]]; then
    echo "$NEW_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    ok "Token updated"
  else
    ok "Keeping existing token"
  fi
else
  echo ""
  info "You need a Jira API token."
  info "Create one at: https://id.atlassian.com/manage-profile/security/api-tokens"
  info "(Click 'Create API token', copy it, paste below)"
  echo ""
  read -rp "→ Paste your Jira API token: " TOKEN
  if [[ -n "$TOKEN" ]]; then
    echo "$TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    ok "Token saved to $TOKEN_FILE"
  else
    warn "No token entered — Jira features won't work until you add one to $TOKEN_FILE"
  fi
fi

echo ""

# ── Step 3: Your Jira board ─────────────────────────────────────

SITE_URL=""
PROJECT_KEY=""
BOARD_ID=""

# Use flag if provided
if [[ -n "$BOARD_URL" ]]; then
  SITE_URL=$(echo "$BOARD_URL" | grep -oE 'https://[^/]+') || true
  PROJECT_KEY=$(echo "$BOARD_URL" | grep -oE '/projects/([^/]+)' | sed 's|/projects/||') || true
  BOARD_ID=$(echo "$BOARD_URL" | grep -oE '/boards/([0-9]+)' | sed 's|/boards/||') || true
fi

# If we have existing values and no flag, offer to keep them
if [[ -z "$SITE_URL" && -n "$EXISTING_SITE_URL" && -n "$EXISTING_PROJECT_KEY" && -n "$EXISTING_BOARD_ID" ]]; then
  echo "  Current board: $EXISTING_PROJECT_KEY (board $EXISTING_BOARD_ID) on $EXISTING_SITE_URL"
  read -rp "→ Keep this board? [Y/n]: " keep_board
  if [[ ! "$keep_board" =~ ^[Nn] ]]; then
    SITE_URL="$EXISTING_SITE_URL"
    PROJECT_KEY="$EXISTING_PROJECT_KEY"
    BOARD_ID="$EXISTING_BOARD_ID"
    ok "Keeping existing board"
  fi
fi

# If still no board, ask for URL
if [[ -z "$SITE_URL" || -z "$PROJECT_KEY" || -z "$BOARD_ID" ]]; then
  echo "  Which Jira board should this instance track?"
  echo "  Open your board in the browser and copy the URL."
  echo "  It looks like: https://YOURSITE.atlassian.net/jira/software/projects/PROJ/boards/123"
  echo ""
  read -rp "→ Paste your Jira board URL: " BOARD_URL

  if [[ -n "$BOARD_URL" ]]; then
    SITE_URL=$(echo "$BOARD_URL" | grep -oE 'https://[^/]+') || true
    PROJECT_KEY=$(echo "$BOARD_URL" | grep -oE '/projects/([^/]+)' | sed 's|/projects/||') || true
    BOARD_ID=$(echo "$BOARD_URL" | grep -oE '/boards/([0-9]+)' | sed 's|/boards/||') || true
  fi

  # Fallback to manual entry if parsing failed
  if [[ -z "$SITE_URL" ]]; then
    read -rp "→ Jira site URL (e.g. https://yoursite.atlassian.net): " SITE_URL
  fi
  if [[ -z "$PROJECT_KEY" ]]; then
    read -rp "→ Project key (e.g. LMSV3): " PROJECT_KEY
  fi
  if [[ -z "$BOARD_ID" ]]; then
    read -rp "→ Board ID (the number in the URL): " BOARD_ID
  fi
fi

[[ -z "$SITE_URL" ]] && { echo "✗ Site URL is required."; exit 1; }
[[ -z "$PROJECT_KEY" ]] && { echo "✗ Project key is required."; exit 1; }
[[ -z "$BOARD_ID" ]] && { echo "✗ Board ID is required."; exit 1; }

ok "Board: $PROJECT_KEY (board $BOARD_ID) on $SITE_URL"

# ── Step 4: Validate Jira auth ──────────────────────────────────

REAL_TOKEN=$(cat "$TOKEN_FILE" 2>/dev/null || true)
if [[ -n "$REAL_TOKEN" ]]; then
  info "Validating Jira credentials..."
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -u "$EMAIL:$REAL_TOKEN" \
    "$SITE_URL/rest/api/3/myself" 2>/dev/null || echo "000")
  if [[ "$HTTP_CODE" == "200" ]]; then
    ok "Jira auth works"
  elif [[ "$HTTP_CODE" == "401" || "$HTTP_CODE" == "403" ]]; then
    warn "Jira auth failed (HTTP $HTTP_CODE) — check your email and token"
    warn "You can fix this later in the Setup page at http://127.0.0.1:8787/setup"
  else
    warn "Could not reach Jira (HTTP $HTTP_CODE) — check the site URL"
  fi
fi

# ── Step 5: Relay server ────────────────────────────────────────

if [[ -z "$RELAY" ]]; then
  RELAY="ssh://uptermd.upterm.dev:22"
fi

echo ""

# ── Step 6: Write configs ───────────────────────────────────────

# operator-settings.yaml (per-machine settings)
SETTINGS_FILE="$ROOT_DIR/operator-settings.yaml"
if [[ -f "$SETTINGS_FILE" ]]; then
  cp "$SETTINGS_FILE" "$SETTINGS_FILE.bak"
fi

cat > "$SETTINGS_FILE" <<YAML
schema_version: 1
jira:
  email: $EMAIL
  token_file: ~/.atlassian-token
llm:
  command: claude
  working_dir: $(pwd)
  max_parallel: 1
sharing:
  upterm_server: "$RELAY"
YAML
ok "Wrote operator-settings.yaml"

# board-routes.yaml — only overwrite if board changed
BOARD_CONFIG="$ROOT_DIR/board-routes.yaml"
BOARD_CHANGED=false
if [[ ! -f "$BOARD_CONFIG" ]]; then
  BOARD_CHANGED=true
elif [[ "$SITE_URL" != "$EXISTING_SITE_URL" || "$PROJECT_KEY" != "$EXISTING_PROJECT_KEY" || "$BOARD_ID" != "$EXISTING_BOARD_ID" ]]; then
  BOARD_CHANGED=true
fi

if $BOARD_CHANGED; then
  if [[ -f "$BOARD_CONFIG" ]]; then
    cp "$BOARD_CONFIG" "$BOARD_CONFIG.bak"
    info "Backed up existing board-routes.yaml"
  fi

  cat > "$BOARD_CONFIG" <<YAML
site_url: $SITE_URL
project_key: $PROJECT_KEY
board_id: '$BOARD_ID'
board_map_path: ./${PROJECT_KEY}.jira-map.yaml
operator_settings_path: ./operator-settings.yaml
poll_interval_minutes: 4
stale_display_minutes: 1440
local_state_dir: ./var/heartbeat
jira:
  email_env: ATLASSIAN_EMAIL
  token_env: ATLASSIAN_TOKEN
  token_file: ~/.atlassian-token
llm:
  command: claude
  args:
  - -p
  - '{prompt}'
  working_dir: $(pwd)
  enabled: true
  dry_run: false
  max_parallel: 1
jira_actions:
  enabled: false
routes: []
YAML
  ok "Wrote board-routes.yaml for $PROJECT_KEY (board $BOARD_ID)"

  # Create empty board map if it doesn't exist
  BOARD_MAP="$ROOT_DIR/${PROJECT_KEY}.jira-map.yaml"
  if [[ ! -f "$BOARD_MAP" ]]; then
    cat > "$BOARD_MAP" <<YAML
# Board map for $PROJECT_KEY
# This will be auto-populated when the heartbeat first runs
status_map: {}
YAML
    ok "Created ${PROJECT_KEY}.jira-map.yaml"
  fi
else
  ok "Board config unchanged"
fi

# ── Step 7: Shell alias ─────────────────────────────────────────

ALIAS_LINE="alias swarmgrid='cd $ROOT_DIR && ./run-web.sh'"
ALIAS_INSTALLED=false

for RC_FILE in "$HOME/.zshrc" "$HOME/.bashrc"; do
  if [[ -f "$RC_FILE" ]]; then
    if ! grep -q "alias swarmgrid=" "$RC_FILE" 2>/dev/null; then
      echo "" >> "$RC_FILE"
      echo "# swarmgrid — start the web dashboard" >> "$RC_FILE"
      echo "$ALIAS_LINE" >> "$RC_FILE"
      ALIAS_INSTALLED=true
    else
      # Update path in case repo moved
      sed -i.tmp "s|alias swarmgrid=.*|${ALIAS_LINE}|" "$RC_FILE" && rm -f "$RC_FILE.tmp"
      ALIAS_INSTALLED=true
    fi
  fi
done

if $ALIAS_INSTALLED; then
  ok "Shell alias 'swarmgrid' ready (new terminal windows)"
else
  warn "Could not find .zshrc or .bashrc — add this manually:"
  echo "  $ALIAS_LINE"
fi

# ── Step 8: Start the web UI ────────────────────────────────────

echo ""
echo "  ✓ Setup complete!"
echo ""
echo "  Starting the web UI..."
echo ""

# Start the web server in background, wait for it, then open browser
source .venv/bin/activate
nohup python -m swarmgrid.cli web --config board-routes.yaml --host 127.0.0.1 --port 8787 \
  > /tmp/swarmgrid-web.log 2>&1 &
WEB_PID=$!

# Wait for server to be ready
for i in $(seq 1 15); do
  if curl -s -o /dev/null http://127.0.0.1:8787/ 2>/dev/null; then
    break
  fi
  sleep 1
done

if curl -s -o /dev/null http://127.0.0.1:8787/ 2>/dev/null; then
  ok "Web UI running at http://127.0.0.1:8787/board"
  echo ""
  echo "  Opening in your browser..."
  if command -v open &>/dev/null; then
    open "http://127.0.0.1:8787/board"
  elif command -v xdg-open &>/dev/null; then
    xdg-open "http://127.0.0.1:8787/board"
  fi
  echo ""
  echo "  Your board is empty — go to the Routes tab to set up"
  echo "  trigger columns for your workflow."
  echo ""
  echo "  Next time, just type:   swarmgrid"
  echo ""
  echo "  Team page:              http://127.0.0.1:8787/team"
  echo "  Setup page:             http://127.0.0.1:8787/setup"
  echo ""
else
  warn "Web server didn't start. Check /tmp/swarmgrid-web.log"
  echo ""
  echo "  Try:     swarmgrid"
  echo "  Or:      ./run-web.sh"
  echo "  Then:    http://127.0.0.1:8787/board"
  echo ""
fi
