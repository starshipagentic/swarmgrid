#!/bin/bash
# Agent demo - runs a visible "agent" in an iTerm2 pane
AGENT_NAME="$1"
CITY="$2"
COLOR="$3"

# ANSI colors
case "$COLOR" in
  red)    C="\033[1;31m" ;;
  green)  C="\033[1;32m" ;;
  yellow) C="\033[1;33m" ;;
  blue)   C="\033[1;34m" ;;
  purple) C="\033[1;35m" ;;
  *)      C="\033[1;37m" ;;
esac
RESET="\033[0m"

clear
echo -e "${C}╔══════════════════════════════════════╗${RESET}"
echo -e "${C}║  AGENT: ${AGENT_NAME}${RESET}"
echo -e "${C}║  TARGET: ${CITY}${RESET}"
echo -e "${C}╚══════════════════════════════════════╝${RESET}"
echo ""
echo -e "${C}[$(date +%H:%M:%S)] Initializing...${RESET}"
sleep 1
echo -e "${C}[$(date +%H:%M:%S)] Searching for weather data...${RESET}"
sleep 1

# Fetch real weather data using wttr.in
echo -e "${C}[$(date +%H:%M:%S)] Contacting weather service...${RESET}"
WEATHER=$(curl -s "wttr.in/${CITY}?format=%t+%C+%h+%w" 2>/dev/null)
sleep 0.5

echo -e "${C}[$(date +%H:%M:%S)] ✓ Data received!${RESET}"
echo ""
echo -e "${C}━━━ OBSERVATION REPORT ━━━${RESET}"
echo -e "${C}  City: ${CITY}${RESET}"
echo -e "${C}  Conditions: ${WEATHER}${RESET}"
echo -e "${C}━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo -e "${C}[$(date +%H:%M:%S)] ✓ Agent ${AGENT_NAME} complete. Standing by.${RESET}"

# Keep pane open
sleep 30
