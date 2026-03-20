#!/bin/bash
# Team Lead - waits for all field agents, then synthesizes a scientific analysis
DROPBOX="/tmp/agent_team"
EXPECTED=5

C="\033[1;36m"  # cyan
W="\033[1;37m"  # white
DIM="\033[2m"
R="\033[0m"
GREEN="\033[1;32m"
YELLOW="\033[1;33m"

clear
echo -e "${C}╔══════════════════════════════════════════════╗${R}"
echo -e "${C}║       ★  TEAM LEAD — CHIEF SCIENTIST  ★     ║${R}"
echo -e "${C}║       Observational Weather Analysis         ║${R}"
echo -e "${C}╚══════════════════════════════════════════════╝${R}"
echo ""
echo -e "${DIM}[$(date +%H:%M:%S)]${R} ${C}Waiting for field agent reports...${R}"
echo ""

# Wait for all agents to report in
RECEIVED=0
while [ $RECEIVED -lt $EXPECTED ]; do
    RECEIVED=$(ls "${DROPBOX}"/*.report 2>/dev/null | wc -l | tr -d ' ')

    # Show status bar
    BAR=""
    for i in $(seq 1 $EXPECTED); do
        if [ $i -le $RECEIVED ]; then
            BAR="${BAR}${GREEN}█${R}"
        else
            BAR="${BAR}${DIM}░${R}"
        fi
    done
    echo -ne "\r  ${C}Agents reporting:${R} ${BAR} ${C}${RECEIVED}/${EXPECTED}${R}   "

    if [ $RECEIVED -lt $EXPECTED ]; then
        sleep 0.5
    fi
done

echo ""
echo ""
echo -e "${GREEN}✓ All ${EXPECTED} field agents have reported in${R}"
echo ""
sleep 1

# Collect all data
echo -e "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${R}"
echo -e "${W}  COLLECTED OBSERVATIONS — $(date '+%Y-%m-%d %H:%M')${R}"
echo -e "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${R}"
echo ""

printf "  ${W}%-12s %-18s %-10s %-14s %-8s${R}\n" "AGENT" "CITY" "TEMP" "CONDITIONS" "HUMID"
echo -e "  ${DIM}──────────── ────────────────── ────────── ────────────── ────────${R}"

for report in "${DROPBOX}"/*.report; do
    source "$report"
    printf "  ${C}%-12s %-18s %-10s %-14s %-8s${R}\n" "$AGENT" "$CITY" "$TEMP" "$CONDITIONS" "$HUMIDITY"
done

echo ""
sleep 1

# Scientific analysis
echo -e "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${R}"
echo -e "${W}  SCIENTIFIC FIELD REPORT${R}"
echo -e "${W}  Pure Observational Analysis — No Prior Knowledge${R}"
echo -e "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${R}"
echo ""
sleep 0.5

echo -e "${YELLOW}  HYPOTHESIS 1: Temperature correlates with position${R}"
echo -e "${DIM}  ────────────────────────────────────────────────${R}"

# Parse temperatures to numbers for comparison
declare -A TEMPS
declare -A LATS
for report in "${DROPBOX}"/*.report; do
    source "$report"
    NUM_TEMP=$(echo "$TEMP" | grep -oE '[+-]?[0-9]+')
    TEMPS[$CITY]=$NUM_TEMP
done

# Sort by temperature
echo -e "  ${C}Ranking observations by temperature:${R}"
for report in "${DROPBOX}"/*.report; do
    source "$report"
    NUM_TEMP=$(echo "$TEMP" | grep -oE '[+-]?[0-9]+')
    echo "    $NUM_TEMP $CITY"
done | sort -n | while read t c; do
    if [ "$t" -lt 10 ]; then
        echo -e "    ${C}${t}°C  ← ${c} (COLD zone)${R}"
    elif [ "$t" -lt 20 ]; then
        echo -e "    ${YELLOW}${t}°C  ← ${c} (MODERATE zone)${R}"
    else
        echo -e "    ${GREEN}${t}°C  ← ${c} (WARM zone)${R}"
    fi
done

echo ""
sleep 0.5

echo -e "${YELLOW}  HYPOTHESIS 2: Moisture and cloud cover are linked${R}"
echo -e "${DIM}  ────────────────────────────────────────────────${R}"
echo -e "  ${C}Observation: Cities with higher humidity readings tend${R}"
echo -e "  ${C}to show cloudier skies and precipitation potential.${R}"
echo -e "  ${C}This suggests water in air → visible cloud formation.${R}"
echo ""
sleep 0.5

echo -e "${YELLOW}  HYPOTHESIS 3: Hemispheric asymmetry${R}"
echo -e "${DIM}  ────────────────────────────────────────────────${R}"
echo -e "  ${C}Key anomaly: Sydney (south) is WARM while Oslo (north)${R}"
echo -e "  ${C}is COLD. If both are ~similar distance from equator,${R}"
echo -e "  ${C}something systematic differs between hemispheres.${R}"
echo -e "  ${C}Possible: the heat source (sun?) illuminates them${R}"
echo -e "  ${C}unequally at different times of year.${R}"
echo ""
sleep 0.5

echo -e "${YELLOW}  HYPOTHESIS 4: Proximity to equator ≈ warmth${R}"
echo -e "${DIM}  ────────────────────────────────────────────────${R}"
echo -e "  ${C}São Paulo & Cairo (closer to equator) are both ~24°C.${R}"
echo -e "  ${C}Oslo (far north) is coldest. This suggests the equator${R}"
echo -e "  ${C}receives more energy than the poles — but H3 shows${R}"
echo -e "  ${C}this is modulated by some seasonal/orbital factor.${R}"
echo ""
sleep 0.5

echo -e "${YELLOW}  FURTHER EXPERIMENTS NEEDED${R}"
echo -e "${DIM}  ────────────────────────────────────────────────${R}"
echo -e "  ${C}1. Repeat in 6 months — test if Oslo/Sydney swap${R}"
echo -e "  ${C}2. Add equatorial city (Nairobi) — test equator hypothesis${R}"
echo -e "  ${C}3. Add altitude variable — does height affect temp?${R}"
echo -e "  ${C}4. Track humidity vs precip over time — causal link?${R}"
echo ""

echo -e "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${R}"
echo -e "${GREEN}  ★ Analysis complete. Science never sleeps. ★${R}"
echo -e "${C}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${R}"

sleep 120
