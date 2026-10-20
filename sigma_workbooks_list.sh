#!/usr/bin/env bash
# List Sigma workbooks with auto-token refresh.
#
# Behavior:
#   1. Source SIGMA_BASE_URL + SIGMA_CLIENT_ID/SECRET from ~/.config/sigma/env
#   2. Mint SIGMA_API_TOKEN via sigma-api/scripts/get-token.sh
#   3. GET /v2/workbooks?limit=<limit> with Authorization: Bearer $SIGMA_API_TOKEN
#   4. If token is rejected (401) or body says "Invalid access/refresh token",
#      re-mint a token and retry once.
#
# Usage:
#   sigma_workbooks_list.sh [--limit N]
#
# Equivalent curl (token already in the environment):
#   curl -sf -H "Authorization: Bearer $SIGMA_API_TOKEN" \
#     "$SIGMA_BASE_URL/v2/workbooks?limit=20" | jq '.entries[] | {id: .workbookId, name}'

set -euo pipefail

limit="20"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit)
      limit="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

SIGMA_ENV="${SIGMA_ENV:-$HOME/.config/sigma/env}"
SIGMA_SKILLS_ROOT="${SIGMA_SKILLS_ROOT:-$HOME/github/sigma-skills}"
GET_TOKEN_SH="${GET_TOKEN_SH:-$SIGMA_SKILLS_ROOT/sigma-api/scripts/get-token.sh}"

if [[ ! -f "$SIGMA_ENV" ]]; then
  echo "Error: missing env file: $SIGMA_ENV" >&2
  exit 1
fi
if [[ ! -f "$GET_TOKEN_SH" ]]; then
  echo "Error: missing get-token script: $GET_TOKEN_SH" >&2
  exit 1
fi

for bin in curl jq bash; do
  command -v "$bin" >/dev/null 2>&1 || { echo "Error: $bin is required" >&2; exit 1; }
done

set -a
# shellcheck disable=SC1090
source "$SIGMA_ENV"
set +a

if [[ -z "${SIGMA_BASE_URL:-}" ]]; then
  echo "Error: SIGMA_BASE_URL is empty in $SIGMA_ENV" >&2
  exit 1
fi

braille_wait() {
  local message=$1
  shift
  if [[ ! -t 2 ]]; then
    "$@"
    return $?
  fi
  local frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
  local i=0
  local spin_pid code=0
  printf '\033[?25l' >&2
  while true; do
    printf '\r\033[K%s %s' "${frames[i]}" "$message" >&2
    i=$(( (i + 1) % ${#frames[@]} ))
    sleep 0.08
  done &
  spin_pid=$!
  "$@" || code=$?
  kill "$spin_pid" 2>/dev/null || true
  wait "$spin_pid" 2>/dev/null || true
  printf '\r\033[K\033[?25h' >&2
  return "$code"
}

refresh_token() {
  # get-token.sh prints: export SIGMA_API_TOKEN=...
  # shellcheck disable=SC1091
  eval "$(braille_wait "Sigma POST /v2/auth/token" bash "$GET_TOKEN_SH")"
}

if [[ -z "${SIGMA_API_TOKEN:-}" ]]; then
  refresh_token
fi

request_workbooks() {
  local url="$1"
  local hdr body status msg code
  hdr="$(mktemp)"
  body="$(mktemp)"

  # Do not use -f; we want the status code to decide whether to refresh.
  braille_wait "Sigma GET /v2/workbooks" curl -sS -D "$hdr" -o "$body" \
    -H "Authorization: Bearer $SIGMA_API_TOKEN" \
    "$url" >/dev/null || true
  status="$(awk 'NR==1{print $2}' "$hdr" | tr -d '\r' || true)"

  msg="$(jq -r '.message // empty' "$body" 2>/dev/null || true)"
  code="$(jq -r '.code // empty' "$body" 2>/dev/null || true)"

  rm -f "$hdr"

  # Export diagnostics for the caller via globals.
  SIGMA_WB_STATUS="$status"
  SIGMA_WB_ERROR_MESSAGE="$msg"
  SIGMA_WB_ERROR_CODE="$code"
  SIGMA_WB_BODY_PATH="$body"
}

url="${SIGMA_BASE_URL}/v2/workbooks?limit=${limit}"

request_workbooks "$url"
if [[ "${SIGMA_WB_STATUS}" == "401" ]] || [[ "${SIGMA_WB_ERROR_MESSAGE}" == *"Invalid access/refresh token"* ]]; then
  refresh_token
  # Retry once.
  request_workbooks "$url"
fi

if [[ "${SIGMA_WB_STATUS}" != "200" ]]; then
  if [[ -n "${SIGMA_WB_ERROR_CODE}" ]]; then
    echo "Sigma API request failed (status=${SIGMA_WB_STATUS}, code=${SIGMA_WB_ERROR_CODE}). Response:" >&2
  else
    echo "Sigma API request failed (status=${SIGMA_WB_STATUS}). Response:" >&2
  fi
  cat "$SIGMA_WB_BODY_PATH" >&2 || true
  rm -f "$SIGMA_WB_BODY_PATH"
  exit 1
fi

jq '.entries[] | {id: .workbookId, name}' "$SIGMA_WB_BODY_PATH"
rm -f "$SIGMA_WB_BODY_PATH"

