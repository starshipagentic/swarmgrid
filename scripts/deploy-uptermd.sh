#!/usr/bin/env bash
set -euo pipefail

#
# Deploy a self-hosted uptermd relay to Fly.io
#
# Prerequisites: none — this script installs flyctl if needed
# Cost: ~$2/month for the smallest always-on machine
#
# Usage:
#   ./scripts/deploy-uptermd.sh              # interactive — prompts for app name
#   ./scripts/deploy-uptermd.sh my-relay      # non-interactive — uses given name
#   ./scripts/deploy-uptermd.sh --status      # check if relay is running
#   ./scripts/deploy-uptermd.sh --destroy     # tear it down
#

APP_NAME="${1:-}"
REGION="${FLY_REGION:-iad}"  # default: Ashburn, VA (US East)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FLY_DIR="$PROJECT_DIR/var/fly-uptermd"

# ── Helpers ──────────────────────────────────────────────────────

info()  { echo "→ $*"; }
error() { echo "✗ $*" >&2; exit 1; }

ensure_flyctl() {
  if command -v flyctl &>/dev/null; then
    return
  elif command -v fly &>/dev/null; then
    return
  fi
  info "Installing flyctl..."
  curl -L https://fly.io/install.sh | sh
  export PATH="$HOME/.fly/bin:$PATH"
}

fly_cmd() {
  if command -v flyctl &>/dev/null; then
    flyctl "$@"
  else
    fly "$@"
  fi
}

load_app_name() {
  if [[ -f "$FLY_DIR/app-name" ]]; then
    APP_NAME="$(cat "$FLY_DIR/app-name")"
  fi
}

# ── Status ───────────────────────────────────────────────────────

if [[ "$APP_NAME" == "--status" ]]; then
  ensure_flyctl
  load_app_name
  [[ -z "$APP_NAME" ]] && error "No deployment found. Run deploy-uptermd.sh first."
  info "App: $APP_NAME"
  fly_cmd status -a "$APP_NAME" 2>&1 || true
  echo ""
  if [[ -f "$FLY_DIR/relay-url" ]]; then
    RELAY_URL="$(cat "$FLY_DIR/relay-url")"
    info "Relay address: $RELAY_URL"
    info ""
    info "For operator-settings.yaml:"
    echo "  sharing:"
    echo "    upterm_server: \"$RELAY_URL\""
    info ""
    info "For teammate setup:"
    echo "  ./scripts/setup-dev.sh --relay \"$RELAY_URL\""
  else
    info "Relay URL not saved. Check Fly.io dashboard for the IP."
  fi
  exit 0
fi

# ── Destroy ──────────────────────────────────────────────────────

if [[ "$APP_NAME" == "--destroy" ]]; then
  ensure_flyctl
  load_app_name
  [[ -z "$APP_NAME" ]] && error "No deployment found."
  info "Destroying $APP_NAME..."
  fly_cmd apps destroy "$APP_NAME" --yes
  rm -rf "$FLY_DIR"
  info "Done. Relay destroyed."
  exit 0
fi

# ── Deploy ───────────────────────────────────────────────────────

ensure_flyctl

# Auth check — signup or login
if ! fly_cmd auth whoami &>/dev/null 2>&1; then
  info "Not logged in to Fly.io."
  info "If you have an account, run: flyctl auth login"
  info "If not, run: flyctl auth signup"
  info ""
  read -rp "Do you have a Fly.io account? [y/N] " has_account
  if [[ "$has_account" =~ ^[Yy] ]]; then
    fly_cmd auth login
  else
    fly_cmd auth signup
  fi
fi

WHOAMI="$(fly_cmd auth whoami 2>/dev/null || echo 'unknown')"
info "Authenticated as: $WHOAMI"

# App name — always confirm with the user
load_app_name
if [[ -n "$APP_NAME" && "${1:-}" != "--rename" ]]; then
  info "Existing deployment found: $APP_NAME"
  echo ""
  echo "  1) Redeploy $APP_NAME"
  echo "  2) Choose a different name"
  echo "  3) Cancel"
  echo ""
  read -rp "→ Choose [1/2/3]: " app_choice
  case "$app_choice" in
    2)
      echo ""
      info "The app name becomes your relay address: <name>.fly.dev"
      info "Pick something your team will recognize (e.g. myteam-uptermd)"
      read -rp "→ New app name: " APP_NAME
      [[ -z "$APP_NAME" ]] && error "App name is required."
      ;;
    3|"") exit 0 ;;
    *) ;; # keep existing
  esac
elif [[ -z "$APP_NAME" ]]; then
  echo ""
  info "The app name becomes your relay address: <name>.fly.dev"
  info "It must be globally unique on Fly.io."
  info "Pick something your team will recognize (e.g. myteam-uptermd)"
  echo ""
  read -rp "→ App name: " APP_NAME
  [[ -z "$APP_NAME" ]] && error "App name is required."
fi

# Create working directory
mkdir -p "$FLY_DIR"
echo "$APP_NAME" > "$FLY_DIR/app-name"

# Write Dockerfile
cat > "$FLY_DIR/Dockerfile" <<'DOCKERFILE'
FROM golang:latest AS builder
WORKDIR /src
RUN git clone --depth 1 https://github.com/owenthereal/upterm.git . && \
    CGO_ENABLED=0 go install ./cmd/...

FROM gcr.io/distroless/static:nonroot
WORKDIR /app
COPY --from=builder /go/bin/uptermd /app/
EXPOSE 2222
ENTRYPOINT ["./uptermd", "--ssh-addr=0.0.0.0:2222"]
DOCKERFILE

# Write fly.toml
cat > "$FLY_DIR/fly.toml" <<TOML
app = "$APP_NAME"
primary_region = "$REGION"
kill_signal = "SIGINT"
kill_timeout = "5s"

[build]
  dockerfile = "Dockerfile"

[[services]]
  protocol = "tcp"
  internal_port = 2222
  auto_stop_machines = false
  auto_start_machines = true
  min_machines_running = 1

  [[services.ports]]
    port = 2222

  [services.concurrency]
    type = "connections"
    hard_limit = 500
    soft_limit = 400

  [[services.tcp_checks]]
    interval = "15s"
    timeout = "2s"
    grace_period = "10s"

[vm]
  cpu_kind = "shared"
  cpus = 1
  memory_mb = 256
TOML

# Create the app if it doesn't exist
if ! fly_cmd status -a "$APP_NAME" &>/dev/null 2>&1; then
  info "Creating app $APP_NAME in region $REGION..."
  (cd "$FLY_DIR" && fly_cmd apps create "$APP_NAME")
fi

# Deploy
info "Deploying uptermd to $APP_NAME.fly.dev..."
(cd "$FLY_DIR" && fly_cmd deploy --ha=false)

# Allocate IPv6 for raw TCP (shared IPv4 only works for HTTP)
info "Allocating IPv6 address for SSH traffic..."
(cd "$FLY_DIR" && fly_cmd ips allocate-v6 -a "$APP_NAME" 2>/dev/null) || true
IPV6="$(cd "$FLY_DIR" && fly_cmd ips list -a "$APP_NAME" 2>/dev/null | grep v6 | awk '{print $2}' || echo '')"

info ""
info "=========================================="
info " Relay deployed: $APP_NAME"
info "=========================================="
RELAY_URL=""
if [[ -n "$IPV6" ]]; then
  RELAY_URL="ssh://[$IPV6]:2222"
else
  RELAY_URL="ssh://$APP_NAME.fly.dev:2222"
fi

# Save relay URL for other scripts
echo "$RELAY_URL" > "$FLY_DIR/relay-url"

# Auto-update operator-settings.yaml if it exists
SETTINGS="$PROJECT_DIR/operator-settings.yaml"
if [[ -f "$SETTINGS" ]]; then
  if grep -q "upterm_server" "$SETTINGS" 2>/dev/null; then
    sed -i '' "s|upterm_server:.*|upterm_server: \"$RELAY_URL\"|" "$SETTINGS"
    info "Updated operator-settings.yaml with relay address"
  elif grep -q "sharing:" "$SETTINGS" 2>/dev/null; then
    sed -i '' "/sharing:/a\\
\  upterm_server: \"$RELAY_URL\"" "$SETTINGS"
    info "Added relay to operator-settings.yaml"
  else
    echo "sharing:" >> "$SETTINGS"
    echo "  upterm_server: \"$RELAY_URL\"" >> "$SETTINGS"
    info "Added sharing config to operator-settings.yaml"
  fi
else
  info "No operator-settings.yaml found. Run setup.sh first, or add manually:"
  info "  sharing:"
  info "    upterm_server: \"$RELAY_URL\""
fi

# Verify relay is reachable
info ""
info "Verifying relay is reachable..."
if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 -p 2222 test@"${IPV6:-$APP_NAME.fly.dev}" 2>&1 | grep -q "Permission denied\|PTY\|Connection closed"; then
  info "Relay is live and accepting SSH connections"
else
  info "Could not verify relay — it may need a moment to start"
fi

info ""
info "=========================================="
info " Relay deployed: $APP_NAME"
info " Address: $RELAY_URL"
info "=========================================="
info ""
if [[ -n "$IPV6" ]]; then
  info "Note: Using IPv6. If any dev's network doesn't support it,"
  info "assign a Dedicated IPv4 (~\$2/month) at:"
  info "  https://fly.io/apps/$APP_NAME/networking"
  info ""
fi
info "Give this to teammates for setup:"
info "  ./scripts/setup-dev.sh --relay \"$RELAY_URL\""
info ""
info "Commands:"
info "  ./scripts/deploy-uptermd.sh --status   # check relay"
info "  ./scripts/deploy-uptermd.sh --destroy  # tear down"
info ""
