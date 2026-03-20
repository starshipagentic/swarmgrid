#!/bin/bash
# Field agent - fetches weather and reports to shared dropbox
AGENT_NAME="$1"
CITY="$2"
COLOR="$3"
DROPBOX="/tmp/agent_team"

case "$COLOR" in
  red)    C="\033[1;31m" ;;
  green)  C="\033[1;32m" ;;
  yellow) C="\033[1;33m" ;;
  blue)   C="\033[1;34m" ;;
  purple) C="\033[1;35m" ;;
  *)      C="\033[1;37m" ;;
esac
R="\033[0m"
DIM="\033[2m"

clear
echo -e "${C}┌──────────────────────────────────────┐${R}"
echo -e "${C}│  FIELD AGENT: ${AGENT_NAME}$(printf '%*s' $((22 - ${#AGENT_NAME})) '')│${R}"
echo -e "${C}│  TARGET: ${CITY}$(printf '%*s' $((27 - ${#CITY})) '')│${R}"
echo -e "${C}└──────────────────────────────────────┘${R}"
echo ""

echo -e "${DIM}[$(date +%H:%M:%S)]${R} ${C}Initializing sensors...${R}"
sleep 0.5

echo -e "${DIM}[$(date +%H:%M:%S)]${R} ${C}Contacting weather station...${R}"
WEATHER=$(curl -s "wttr.in/${CITY}?format=%t|%C|%h|%w|%p" 2>/dev/null)
TEMP=$(echo "$WEATHER" | cut -d'|' -f1)
COND=$(echo "$WEATHER" | cut -d'|' -f2)
HUMID=$(echo "$WEATHER" | cut -d'|' -f3)
WIND=$(echo "$WEATHER" | cut -d'|' -f4)
PRECIP=$(echo "$WEATHER" | cut -d'|' -f5)

echo -e "${DIM}[$(date +%H:%M:%S)]${R} ${C}✓ Signal acquired${R}"
sleep 0.3

echo ""
echo -e "${C}┌─── OBSERVATION ───────────────────────┐${R}"
echo -e "${C}│ Temp:     ${TEMP}$(printf '%*s' $((27 - ${#TEMP})) '')│${R}"
echo -e "${C}│ Sky:      ${COND}$(printf '%*s' $((27 - ${#COND})) '')│${R}"
echo -e "${C}│ Humidity: ${HUMID}$(printf '%*s' $((27 - ${#HUMID})) '')│${R}"
echo -e "${C}│ Wind:     ${WIND}$(printf '%*s' $((27 - ${#WIND})) '')│${R}"
echo -e "${C}│ Precip:   ${PRECIP}$(printf '%*s' $((27 - ${#PRECIP})) '')│${R}"
echo -e "${C}└───────────────────────────────────────┘${R}"

# Write report to shared dropbox
cat > "${DROPBOX}/${AGENT_NAME}.report" <<EOF
AGENT=${AGENT_NAME}
CITY=${CITY}
TEMP=${TEMP}
CONDITIONS=${COND}
HUMIDITY=${HUMID}
WIND=${WIND}
PRECIP=${PRECIP}
TIMESTAMP=$(date +%H:%M:%S)
EOF

echo ""
echo -e "${DIM}[$(date +%H:%M:%S)]${R} ${C}✓ Report filed to dropbox${R}"
echo -e "${DIM}[$(date +%H:%M:%S)]${R} ${C}✓ Standing by for team lead${R}"

# Stay alive
sleep 120
