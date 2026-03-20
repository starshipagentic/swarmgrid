#!/usr/bin/env bash
# SwarmGrid installer — curl -fsSL swarmgrid.org/install | sh
#
# What it does:
#   1. Detect OS (macOS / Linux)
#   2. Check for Python 3.11+, install if needed
#   3. Check for tmux, install if needed
#   4. Check for upterm, install if needed
#   5. Create venv at ~/.swarmgrid/venv
#   4. pip install swarmgrid
#   5. Prompt for API key
#   6. Write ~/.swarmgrid/config.yaml
#   7. macOS: install LaunchAgent for auto-start
#      Linux: install systemd user service
#   8. Start the agent
#
# Uninstall: swarmgrid-uninstall (installed alongside)

set -euo pipefail

SWARMGRID_DIR="$HOME/.swarmgrid"
VENV_DIR="$SWARMGRID_DIR/venv"
CONFIG_FILE="$SWARMGRID_DIR/config.yaml"
BIN_LINK="/usr/local/bin/swarmgrid"
MIN_PYTHON="3.11"

# -- Colors (disabled when piped) --
if [ -t 1 ]; then
  BOLD='\033[1m'
  GREEN='\033[0;32m'
  YELLOW='\033[0;33m'
  RED='\033[0;31m'
  RESET='\033[0m'
else
  BOLD='' GREEN='' YELLOW='' RED='' RESET=''
fi

info()  { printf "${GREEN}[swarmgrid]${RESET} %s\n" "$*"; }
warn()  { printf "${YELLOW}[swarmgrid]${RESET} %s\n" "$*"; }
error() { printf "${RED}[swarmgrid]${RESET} %s\n" "$*" >&2; }
fatal() { error "$@"; exit 1; }

# -- OS detection --
detect_os() {
  case "$(uname -s)" in
    Darwin) echo "macos" ;;
    Linux)  echo "linux" ;;
    *)      fatal "Unsupported OS: $(uname -s). SwarmGrid supports macOS and Linux." ;;
  esac
}

# -- Python detection --
# Returns path to a suitable python3, or empty string
find_python() {
  for cmd in python3.13 python3.12 python3.11 python3; do
    local py
    py=$(command -v "$cmd" 2>/dev/null || true)
    if [ -n "$py" ]; then
      if "$py" -c "import sys; exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
        echo "$py"
        return
      fi
    fi
  done
  echo ""
}

install_python_macos() {
  if command -v brew &>/dev/null; then
    info "Installing Python via Homebrew..."
    brew install python@3.12
  else
    fatal "Python $MIN_PYTHON+ not found and Homebrew is not installed.\nInstall Python from https://python.org/downloads or run:\n  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
  fi
}

install_python_linux() {
  if command -v apt-get &>/dev/null; then
    info "Installing Python via apt..."
    sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-venv python3-pip
  elif command -v dnf &>/dev/null; then
    info "Installing Python via dnf..."
    sudo dnf install -y python3 python3-pip
  elif command -v pacman &>/dev/null; then
    info "Installing Python via pacman..."
    sudo pacman -Sy --noconfirm python python-pip
  else
    fatal "Python $MIN_PYTHON+ not found. Please install Python 3.11+ manually."
  fi
}

ensure_python() {
  local py
  py=$(find_python)
  if [ -n "$py" ]; then
    echo "$py"
    return
  fi

  warn "Python $MIN_PYTHON+ not found. Attempting to install..."
  local os_type="$1"
  if [ "$os_type" = "macos" ]; then
    install_python_macos
  else
    install_python_linux
  fi

  py=$(find_python)
  if [ -z "$py" ]; then
    fatal "Failed to install Python $MIN_PYTHON+. Please install it manually."
  fi
  echo "$py"
}

# -- tmux detection + install --
ensure_tmux() {
  local os_type="$1"
  if command -v tmux &>/dev/null; then
    info "tmux found: $(tmux -V 2>&1)"
    return
  fi

  warn "tmux not found. Attempting to install..."
  if [ "$os_type" = "macos" ]; then
    if command -v brew &>/dev/null; then
      brew install tmux
    else
      fatal "tmux not found and Homebrew is not installed. Install tmux manually."
    fi
  else
    if command -v apt-get &>/dev/null; then
      sudo apt-get update -qq && sudo apt-get install -y -qq tmux
    elif command -v dnf &>/dev/null; then
      sudo dnf install -y tmux
    elif command -v pacman &>/dev/null; then
      sudo pacman -Sy --noconfirm tmux
    else
      fatal "tmux not found. Please install tmux manually."
    fi
  fi

  if ! command -v tmux &>/dev/null; then
    fatal "Failed to install tmux."
  fi
  info "tmux installed: $(tmux -V 2>&1)"
}

# -- upterm detection + install --
ensure_upterm() {
  local os_type="$1"
  if command -v upterm &>/dev/null; then
    info "upterm found: $(upterm version 2>&1 | head -1)"
    return
  fi

  warn "upterm not found. Attempting to install..."
  if [ "$os_type" = "macos" ]; then
    if command -v brew &>/dev/null; then
      brew install owenthereal/upterm/upterm
    else
      fatal "upterm not found and Homebrew is not installed. See https://github.com/owenthereal/upterm#installation"
    fi
  else
    # Linux: download binary from GitHub releases
    local arch
    arch=$(uname -m)
    case "$arch" in
      x86_64)  arch="amd64" ;;
      aarch64) arch="arm64" ;;
      armv7l)  arch="arm" ;;
      *)       fatal "Unsupported architecture for upterm: $arch" ;;
    esac

    local version="0.13.2"
    local url="https://github.com/owenthereal/upterm/releases/download/v${version}/upterm_linux_${arch}.tar.gz"
    info "Downloading upterm from $url..."
    local tmpdir
    tmpdir=$(mktemp -d)
    curl -fsSL "$url" -o "$tmpdir/upterm.tar.gz"
    tar -xzf "$tmpdir/upterm.tar.gz" -C "$tmpdir"
    sudo install -m 755 "$tmpdir/upterm" /usr/local/bin/upterm
    rm -rf "$tmpdir"
  fi

  if ! command -v upterm &>/dev/null; then
    fatal "Failed to install upterm. See https://github.com/owenthereal/upterm#installation"
  fi
  info "upterm installed: $(upterm version 2>&1 | head -1)"
}

# -- Main install --
main() {
  printf "\n${BOLD}SwarmGrid Installer${RESET}\n"
  printf "Cloud-orchestrated, edge-powered agent platform\n\n"

  local os_type
  os_type=$(detect_os)
  info "Detected OS: $os_type"

  # 1. Ensure Python
  local python
  python=$(ensure_python "$os_type")
  info "Using Python: $python ($($python --version 2>&1))"

  # 2. Ensure tmux + upterm
  ensure_tmux "$os_type"
  ensure_upterm "$os_type"

  # 3. Create venv
  mkdir -p "$SWARMGRID_DIR"
  if [ ! -d "$VENV_DIR" ]; then
    info "Creating virtual environment at $VENV_DIR..."
    "$python" -m venv "$VENV_DIR"
  else
    info "Virtual environment already exists at $VENV_DIR"
  fi

  local pip="$VENV_DIR/bin/pip"
  local sg_python="$VENV_DIR/bin/python"

  # 3. Install/upgrade swarmgrid
  info "Installing swarmgrid..."
  "$pip" install --upgrade --quiet pip setuptools wheel
  "$pip" install --upgrade --quiet swarmgrid

  # Verify installation
  if ! "$VENV_DIR/bin/swarmgrid" --help &>/dev/null; then
    fatal "Installation failed — swarmgrid command not working"
  fi
  info "swarmgrid installed successfully"

  # 4. Symlink to PATH
  if [ -w "$(dirname "$BIN_LINK")" ] || [ ! -e "$BIN_LINK" ]; then
    ln -sf "$VENV_DIR/bin/swarmgrid" "$BIN_LINK" 2>/dev/null || true
  fi
  # Also add to ~/.local/bin (common on Linux)
  mkdir -p "$HOME/.local/bin"
  ln -sf "$VENV_DIR/bin/swarmgrid" "$HOME/.local/bin/swarmgrid" 2>/dev/null || true

  # 5. API key setup
  if [ -f "$CONFIG_FILE" ]; then
    info "Config already exists at $CONFIG_FILE"
  else
    printf "\n"
    printf "${BOLD}API Key Setup${RESET}\n"
    printf "Get your API key from your SwarmGrid dashboard (Settings tab).\n\n"

    local api_key=""
    if [ -t 0 ]; then
      # Interactive — prompt for key
      printf "API key (paste and press Enter): "
      read -r api_key
    fi

    if [ -z "$api_key" ]; then
      warn "No API key provided. You can set it later in $CONFIG_FILE"
      api_key="YOUR_API_KEY_HERE"
    fi

    cat > "$CONFIG_FILE" << YAML
# SwarmGrid edge agent configuration
# Generated by install script

api_key: "$api_key"
cloud_url: "https://swarmgrid.org"
YAML
    chmod 600 "$CONFIG_FILE"
    info "Config written to $CONFIG_FILE"
  fi

  # 6. macOS: copy .app if present
  if [ "$os_type" = "macos" ]; then
    local app_bundle="$VENV_DIR/share/SwarmGrid.app"
    if [ -d "$app_bundle" ]; then
      info "Installing SwarmGrid.app to /Applications..."
      cp -R "$app_bundle" /Applications/SwarmGrid.app 2>/dev/null || true
    fi
  fi

  # 7. OS-specific daemon setup
  if [ "$os_type" = "macos" ]; then
    setup_macos_launchagent
  else
    setup_linux_systemd
  fi

  # 8. Start the agent
  info "Starting SwarmGrid agent..."
  if [ "$os_type" = "macos" ]; then
    launchctl load "$HOME/Library/LaunchAgents/org.swarmgrid.agent.plist" 2>/dev/null || true
  else
    systemctl --user start swarmgrid-agent.service 2>/dev/null || true
  fi

  # Done
  printf "\n${GREEN}${BOLD}SwarmGrid is running. Open swarmgrid.org to see your dashboard.${RESET}\n\n"
  printf "  Dashboard:  https://swarmgrid.org\n"
  printf "  Config:     $CONFIG_FILE\n"
  printf "  Logs:       /tmp/swarmgrid-agent-upterm.log\n"
  printf "  Status:     swarmgrid agent --background\n"
  printf "\n"
  if [ "$os_type" = "macos" ]; then
    printf "To stop:      launchctl unload ~/Library/LaunchAgents/org.swarmgrid.agent.plist\n"
  else
    printf "To stop:      systemctl --user stop swarmgrid-agent\n"
  fi
  printf "To uninstall: rm -rf ~/.swarmgrid && rm -f $BIN_LINK\n"
  printf "\n"
}

setup_macos_launchagent() {
  local plist_dir="$HOME/Library/LaunchAgents"
  local plist_file="$plist_dir/org.swarmgrid.agent.plist"

  mkdir -p "$plist_dir"

  if [ -f "$plist_file" ]; then
    # Unload existing before overwriting
    launchctl unload "$plist_file" 2>/dev/null || true
  fi

  cat > "$plist_file" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>org.swarmgrid.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>${VENV_DIR}/bin/swarmgrid</string>
        <string>agent</string>
        <string>--background</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${SWARMGRID_DIR}/agent.log</string>
    <key>StandardErrorPath</key>
    <string>${SWARMGRID_DIR}/agent.err</string>
    <key>WorkingDirectory</key>
    <string>${HOME}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
PLIST

  info "LaunchAgent installed at $plist_file"
  info "Agent will auto-start on login"
}

setup_linux_systemd() {
  local service_dir="$HOME/.config/systemd/user"
  local service_file="$service_dir/swarmgrid-agent.service"

  mkdir -p "$service_dir"

  cat > "$service_file" << UNIT
[Unit]
Description=SwarmGrid Edge Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${VENV_DIR}/bin/swarmgrid agent
Restart=on-failure
RestartSec=10
WorkingDirectory=${HOME}
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=HOME=${HOME}

[Install]
WantedBy=default.target
UNIT

  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable swarmgrid-agent.service 2>/dev/null || true

  info "systemd user service installed at $service_file"
  info "Agent will auto-start on login"
}

main "$@"
