#!/usr/bin/env bash
# de daily-pulse — legacy entry; delegates to bi-applications scripts/run-local.sh.
set -euo pipefail

# Legacy wrapper — prefer `de daily-pulse` (bi-applications scripts/run-local.sh).
ROOT="${BI_APPLICATIONS_HOME:-${DAILY_PULSE_HOME:-$HOME/github/bi-applications}}"
RUN_LOCAL="$ROOT/scripts/run-local.sh"
if [[ ! -x "$RUN_LOCAL" ]]; then
  echo "ERROR: missing $RUN_LOCAL" >&2
  echo "Fix: set bi_applications in ~/.config/de/config.toml [paths]" >&2
  exit 1
fi

exec "$RUN_LOCAL" --dry-run "$@"
