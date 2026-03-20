#!/usr/bin/env bash
# hub-test.sh — Manual POC test for the hub CGI-over-SSH model.
#
# Usage:
#   ./scripts/hub-test.sh                # runs all tests using local handler
#   ./scripts/hub-test.sh --ssh <connect> # test via SSH (e.g., "ssh abc@upterm.dev -p 22")
#
# Tests: ping, checkin, list, whoami
# Verifies SQLite state after each step.

set -euo pipefail

HANDLER="$(cd "$(dirname "$0")/.." && pwd)/src/swarmgrid/hub_handler.py"
DB_DIR="$(cd "$(dirname "$0")/.." && pwd)/var/hub"
DB_PATH="$DB_DIR/hub.sqlite"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

pass=0
fail=0

send_cmd() {
  local json="$1"
  if [[ -n "${SSH_CONNECT:-}" ]]; then
    echo "$json" | $SSH_CONNECT 2>/dev/null
  else
    echo "$json" | python3 "$HANDLER"
  fi
}

check() {
  local label="$1"
  local result="$2"
  local expected="$3"

  if echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if $expected else 1)" 2>/dev/null; then
    echo -e "  ${GREEN}PASS${NC} $label"
    pass=$((pass + 1))
  else
    echo -e "  ${RED}FAIL${NC} $label"
    echo "    Got: $result"
    fail=$((fail + 1))
  fi
}

# Parse args
SSH_CONNECT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh)
      shift
      SSH_CONNECT="$1"
      shift
      ;;
    *)
      echo "Unknown arg: $1"
      exit 1
      ;;
  esac
done

echo -e "${YELLOW}Hub Handler POC Test${NC}"
echo "========================"
if [[ -n "$SSH_CONNECT" ]]; then
  echo "Mode: SSH ($SSH_CONNECT)"
else
  echo "Mode: Local (direct python)"
fi
echo ""

# Clean slate for local tests
if [[ -z "$SSH_CONNECT" && -f "$DB_PATH" ]]; then
  echo "Removing old test DB..."
  rm -f "$DB_PATH"
fi

# Test 1: ping
echo "Test 1: ping"
result=$(send_cmd '{"cmd":"ping"}')
check "returns ok" "$result" 'd.get("ok") == True'
check "returns pong" "$result" 'd.get("pong") == True'

# Test 2: checkin with simple keys
echo ""
echo "Test 2: checkin (simple keys)"
result=$(send_cmd '{"cmd":"checkin","dev_id":"travis","tickets":["PROJ-100","PROJ-101"]}')
check "returns ok" "$result" 'd.get("ok") == True'
check "checked_in == 2" "$result" 'd.get("checked_in") == 2'

# Test 3: checkin with rich objects
echo ""
echo "Test 3: checkin (rich objects)"
result=$(send_cmd '{"cmd":"checkin","dev_id":"karthik","tickets":[{"key":"PROJ-200","summary":"Fix auth","status":"In Progress"}]}')
check "returns ok" "$result" 'd.get("ok") == True'
check "checked_in == 1" "$result" 'd.get("checked_in") == 1'

# Test 4: list
echo ""
echo "Test 4: list"
result=$(send_cmd '{"cmd":"list"}')
check "returns ok" "$result" 'd.get("ok") == True'
check "has checkins" "$result" 'len(d.get("checkins",[])) >= 3'
check "travis present" "$result" 'any(c["dev_id"]=="travis" for c in d.get("checkins",[]))'
check "karthik present" "$result" 'any(c["dev_id"]=="karthik" for c in d.get("checkins",[]))'

# Test 5: whoami
echo ""
echo "Test 5: whoami"
result=$(send_cmd '{"cmd":"whoami"}')
check "returns ok" "$result" 'd.get("ok") == True'
check "has ssh_client" "$result" '"ssh_client" in d'

# Test 6: unknown command
echo ""
echo "Test 6: unknown command"
result=$(send_cmd '{"cmd":"nope"}')
check "returns error" "$result" 'd.get("ok") == False'

# Test 7: empty input
echo ""
echo "Test 7: empty input"
result=$(echo "" | python3 "$HANDLER" 2>/dev/null || true)
if [[ -z "$SSH_CONNECT" ]]; then
  check "handles empty" "$result" 'd.get("ok") == False'
else
  echo -e "  ${YELLOW}SKIP${NC} (SSH mode)"
fi

# Verify SQLite
if [[ -z "$SSH_CONNECT" && -f "$DB_PATH" ]]; then
  echo ""
  echo "SQLite verification:"
  count=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM checkins;")
  echo "  Total rows: $count"
  echo "  Unique devs: $(sqlite3 "$DB_PATH" "SELECT COUNT(DISTINCT dev_id) FROM checkins;")"
  echo "  Sample: $(sqlite3 "$DB_PATH" "SELECT dev_id, ticket_key, status FROM checkins LIMIT 3;")"
fi

echo ""
echo "========================"
echo -e "Results: ${GREEN}${pass} passed${NC}, ${RED}${fail} failed${NC}"
[[ $fail -eq 0 ]] && exit 0 || exit 1
