#!/bin/bash
# Read-only weekend readiness for the CourtReserve LaunchAgent.
set -u

LABEL="com.shawn.courtbooking"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
BOOKING_SCRIPT="${HOME}/bin/run_booking.sh"
DEFAULTS="${HOME}/github/courtreserve/reservation_defaults.json"
LOG="${HOME}/logs/courtbooking.log"
ERR_LOG="${HOME}/logs/courtbooking-error.log"
DOMAIN="gui/$(id -u)/${LABEL}"

section() {
  printf '\n%s\n' "$1"
}

echo "🎾 COURTRESERVE WEEKEND READINESS"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Current time: $(date)"

section "1. SCHEDULER STATUS"
if print_out="$(launchctl print "$DOMAIN" 2>&1)"; then
  printf '%s\n' "$print_out" | awk '
    /^\t(state|runs|last exit code) = / { print }
  '
  echo "schedule:"
  printf '%s\n' "$print_out" | awk '
    /"Weekday" =>/ { wd = $3 }
    /"Hour" =>/ { hour = $3 }
    /"Minute" =>/ { minute = $3 }
    /"Weekday" =>/ && hour != "" && minute != "" {
      name = wd
      if (wd == 0 || wd == 7) name = "Sun"
      else if (wd == 1) name = "Mon"
      else if (wd == 2) name = "Tue"
      else if (wd == 3) name = "Wed"
      else if (wd == 4) name = "Thu"
      else if (wd == 5) name = "Fri"
      else if (wd == 6) name = "Sat"
      printf "\t%s %s:%02d (Weekday %s)\n", name, hour, minute, wd
      hour = ""; minute = ""
    }
  '
else
  echo "launchctl print failed for ${DOMAIN}"
  printf '%s\n' "$print_out"
fi

section "2. SCHEDULER ENABLED"
if [[ -f "$PLIST" ]] && /usr/libexec/PlistBuddy -c 'Print :Disabled' "$PLIST" >/dev/null 2>&1; then
  echo "plist Disabled = $(/usr/libexec/PlistBuddy -c 'Print :Disabled' "$PLIST")"
elif printf '%s\n' "${print_out:-}" | grep -q 'state = disabled'; then
  echo "launchctl state = disabled"
else
  echo "No disabled override found"
fi

section "3. BOOKING SCRIPT"
if [[ -f "$BOOKING_SCRIPT" ]]; then
  cat "$BOOKING_SCRIPT"
else
  echo "missing ${BOOKING_SCRIPT}"
fi

section "4. RESERVATION DEFAULTS"
if [[ -f "$DEFAULTS" ]]; then
  cat "$DEFAULTS"
else
  echo "missing ${DEFAULTS}"
fi

section "5. LAST BOOKING ACTIVITY"
if [[ -f "$LOG" ]]; then
  tail -n 40 "$LOG"
else
  echo "missing ${LOG}"
fi

section "6. RECENT ERRORS"
if [[ -s "$ERR_LOG" ]]; then
  tail -n 40 "$ERR_LOG"
fi

section "7. MAC SLEEP SETTINGS"
pmset -g custom | awk '
  /^Battery Power:/ || /^AC Power:/ { print; next }
  /displaysleep|networkoversleep|disksleep/ { print; next }
  /^[[:space:]]*sleep[[:space:]]/ { print }
'

section "8. ACTIVE BOOKING PROCESSES"
ps -ax -o pid=,command= | awk '/run_booking\.sh|courtreserve\/.*main\.py/ && !/awk/ { print }'
