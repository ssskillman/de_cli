#!/usr/bin/env bash
# Daily Slack FAQ pipeline: sync channel C01V64XA4AK then regenerate reports.
set -euo pipefail

DE_ROOT="${DE_HOME:-$HOME/de}"
CHANNEL="${SLACK_FAQ_CHANNEL:-C01V64XA4AK}"
LOG_DIR="${SLACK_FAQ_LOG_DIR:-$HOME/.local/share/de/slack_faq/logs}"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOG_DIR/daily_${STAMP}.log"

exec > >(tee -a "$LOG") 2>&1

echo "=== slack_faq_daily start $(date -u +%Y-%m-%dT%H:%M:%SZ) channel=$CHANNEL ==="

python3 "$DE_ROOT/scripts/slack_channel_sync.py" --channel "$CHANNEL"
python3 "$DE_ROOT/scripts/slack_faq_report.py" --channel "$CHANNEL" --include-non-questions

echo "=== slack_faq_daily done ==="
