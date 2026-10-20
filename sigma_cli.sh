#!/usr/bin/env bash
# de sigma — mint a Sigma API token, then run the Sigma Computing CLI.
#
# Docs:
#   https://help.sigmacomputing.com/docs/install-and-configure-the-sigma-cli
#   https://help.sigmacomputing.com/docs/use-the-sigma-cli
#
# Auth (headless, takes precedence over stored CLI profiles):
#   source ~/.config/sigma/env → SIGMA_BASE_URL / SIGMA_CLIENT_ID / SIGMA_CLIENT_SECRET
#   mint SIGMA_API_TOKEN via sigma-skills get-token.sh
#
# Usage:
#   de sigma                         # interactive prompt (sigma>)
#   de sigma --help
#   de sigma api workbooks list
#   de sigma api workbooks list --params '{"limit":2}'
#
# At the sigma> prompt you can type:
#   --help
#   sigma --help
#   api workbooks list
#   exit

set -euo pipefail

SIGMA_ENV="${SIGMA_ENV:-$HOME/.config/sigma/env}"
SIGMA_SKILLS_ROOT="${SIGMA_SKILLS_ROOT:-$HOME/github/sigma-skills}"
GET_TOKEN_SH="${GET_TOKEN_SH:-$SIGMA_SKILLS_ROOT/sigma-api/scripts/get-token.sh}"
BOOTSTRAP_PY="${BOOTSTRAP_PY:-$HOME/de/sigma_cli_bootstrap.py}"
DE_SIGMA_PROFILE="${DE_SIGMA_PROFILE:-de}"
export DE_SIGMA_PROFILE

export PATH="$HOME/.sigma-cli/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

resolve_sigma() {
  if [[ -n "${SIGMA_BIN:-}" && -x "$SIGMA_BIN" ]]; then
    printf '%s\n' "$SIGMA_BIN"
    return 0
  fi
  if command -v sigma >/dev/null 2>&1; then
    command -v sigma
    return 0
  fi
  if [[ -x "$HOME/.sigma-cli/bin/sigma" ]]; then
    printf '%s\n' "$HOME/.sigma-cli/bin/sigma"
    return 0
  fi
  return 1
}

if [[ ! -f "$SIGMA_ENV" ]]; then
  echo "Error: missing env file: $SIGMA_ENV" >&2
  echo "Create it with SIGMA_BASE_URL, SIGMA_CLIENT_ID, SIGMA_CLIENT_SECRET." >&2
  exit 1
fi
if [[ ! -f "$GET_TOKEN_SH" ]]; then
  echo "Error: missing get-token script: $GET_TOKEN_SH" >&2
  exit 1
fi

SIGMA_BIN="$(resolve_sigma || true)"
if [[ -z "$SIGMA_BIN" ]]; then
  echo "Error: Sigma CLI not found." >&2
  echo "Install with either:" >&2
  echo "  brew install sigmacomputing/tap/sigma-computing-cli" >&2
  echo "  curl --proto '=https' --tlsv1.2 -LsSf https://assets.sigmacomputing.com/sigma-cli/releases/latest/sigma-cli-installer.sh | sh" >&2
  echo "Then add ~/.sigma-cli/bin to PATH if needed." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$SIGMA_ENV"
set +a

if [[ -z "${SIGMA_BASE_URL:-}" || -z "${SIGMA_CLIENT_ID:-}" || -z "${SIGMA_CLIENT_SECRET:-}" ]]; then
  echo "Error: SIGMA_BASE_URL, SIGMA_CLIENT_ID, and SIGMA_CLIENT_SECRET must be set in $SIGMA_ENV" >&2
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
  eval "$(braille_wait "Sigma POST /v2/auth/token" bash "$GET_TOKEN_SH")"
  echo "Sigma token minted (expires ~1h)." >&2
}

refresh_token

profile_ok() {
  local out
  out="$("$SIGMA_BIN" -p "$DE_SIGMA_PROFILE" auth status 2>&1)" || true
  [[ "$out" != *"no default profile"* && "$out" != *"error"* ]]
}

ensure_profile() {
  # sigma 0.2.1 requires a stored profile even when SIGMA_* env vars are set.
  if profile_ok; then
    return 0
  fi
  python3 "$BOOTSTRAP_PY" "$SIGMA_BIN"
  "$SIGMA_BIN" auth set-default "$DE_SIGMA_PROFILE" >/dev/null 2>&1 || true
  if ! profile_ok; then
    echo "Error: Sigma CLI profile '$DE_SIGMA_PROFILE' was not created. Run: sigma auth login" >&2
    return 1
  fi
}

run_sigma() {
  local code=0
  local -a argv=("$@")
  local a inject=1
  for a in "${argv[@]}"; do
    case "$a" in
      -p|--profile|--profile=*) inject=0 ;;
    esac
  done
  if [[ "${argv[0]:-}" == "auth" && "${argv[1]:-}" == "login" ]]; then
    inject=0
  fi
  if [[ "$inject" -eq 1 ]]; then
    argv=(-p "$DE_SIGMA_PROFILE" "${argv[@]}")
  fi
  braille_wait "Sigma ${argv[*]}" "$SIGMA_BIN" "${argv[@]}" || code=$?
  # Exit 2 = authentication error — re-mint and retry once.
  if [[ "$code" -eq 2 ]]; then
    echo "Sigma CLI auth failed (exit 2) — refreshing token and retrying once…" >&2
    refresh_token
    code=0
    braille_wait "Sigma ${argv[*]}" "$SIGMA_BIN" "${argv[@]}" || code=$?
  fi
  return "$code"
}

ensure_profile

if [[ $# -gt 0 ]]; then
  run_sigma "$@"
  exit $?
fi

if [[ ! -t 0 ]]; then
  echo "No TTY. Pass a Sigma CLI command, e.g.:" >&2
  echo "  de sigma --help" >&2
  echo "  de sigma api workbooks list" >&2
  exit 1
fi

echo "Sigma CLI  ($SIGMA_BIN)" >&2
echo "Host:      $SIGMA_BASE_URL" >&2
echo "Prompt:    type a sigma command. Examples:" >&2
echo "             --help" >&2
echo "             sigma --help" >&2
echo "             api workbooks list" >&2
echo "             api workbooks list --params '{\"limit\":20}'" >&2
echo "           Same as:" >&2
echo "             curl -sf -H \"Authorization: Bearer \$SIGMA_API_TOKEN\" \\" >&2
echo "               \"\$SIGMA_BASE_URL/v2/workbooks?limit=20\" | jq '.entries[] | {id: .workbookId, name}'" >&2
echo "           exit / quit to leave." >&2

while true; do
  # -e enables readline history when the shell supports it.
  if ! IFS= read -e -r -p "sigma> " line; then
    echo >&2
    break
  fi
  # Trim leading/trailing whitespace
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [[ -z "$line" ]] && continue
  case "$line" in
    exit|quit|q) break ;;
    help) line="--help" ;;
  esac
  # Allow typing the binary name as in a normal shell.
  if [[ "$line" == sigma[[:space:]]* ]]; then
    line="${line#sigma}"
    line="${line#"${line%%[![:space:]]*}"}"
  elif [[ "$line" == "sigma" ]]; then
    line="--help"
  fi

  # Quote-aware split so --params '{"limit":2}' works. Trusted local input only.
  args=()
  # shellcheck disable=SC2206
  eval "args=( $line )"
  run_sigma "${args[@]}" || true
done
exit 0
