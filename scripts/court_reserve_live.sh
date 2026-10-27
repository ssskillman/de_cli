#!/bin/bash

echo "🎾 COURTRESERVE LIVE STATUS"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━"

launchctl print "gui/$(id -u)/com.shawn.courtbooking" \
  | grep -E 'state =|runs =|last exit code' \
  | head -3

echo ""
echo "Booking process:"
# shellcheck disable=SC2009
ps -axo pid,etime,command \
  | grep -E '[p]ython .*courtreserve/main.py|[r]un_booking.sh'

echo ""
echo "Latest activity:"
tail -n 5 ~/logs/courtbooking.log
