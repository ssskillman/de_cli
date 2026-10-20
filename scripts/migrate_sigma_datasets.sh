#!/usr/bin/env bash
set -Eeuo pipefail

# Bulk migrate Sigma datasets to data models using the official Sigma CLI.
# Defaults to dry-run. Use --execute to perform migrations.

usage() {
  cat <<'EOF'
Usage:
  de sigma-dataset-migrate [MODE] [OPTIONS]
  ~/de/scripts/migrate_sigma_datasets.sh [MODE] [OPTIONS]

Bulk migrate Sigma datasets to data models through the official Sigma CLI.
The default mode is --dry-run; migrations occur only with --execute.

Options:
  --audit         Show the migration inventory overview; make no migration calls
  --show-all      Audit and list every dataset needing migration with its owner
  --show-reports  Audit and list reports/workbooks using migration targets
  --dry-run       Validate each eligible migration without changing Sigma (default)
  --execute       Perform migrations and verify each resulting dataset status
  --owner PERSON  Limit targets by owner email, name, member ID, or unique match
  --report REPORT Limit targets to datasets used by one report/workbook; accepts
                  an exact name, path, URL, ID, or a unique partial match
  --badge-errors  After execution, use fzf to review workbooks affected by failed
                  datasets and verify Warning/Deprecated badges set in Sigma
                  (--flag-errors alias)
  --verbose       Show every dataset name and migration response detail
  -h, --help      Show this help

Environment:
  SIGMA_PROFILE=de             Sigma CLI profile (default: DE_SIGMA_PROFILE or de)
  WAIT_SECONDS=5               Pause between datasets
  UPDATE_REFERENCES=true       Update workbook references to the data model
  INCLUDE_NOT_REQUIRED=false   Include datasets marked not-required
  LOG_FILE=path.jsonl          JSONL audit log path
  REPORT_SCAN_CONCURRENCY=100  Parallel requests used by dependency scans
  SIGMA_ENV=~/.config/sigma/env
  SIGMA_BIN=/path/to/sigma
EOF
}

MODE="dry-run"
REQUESTED_MODE=""
AUDIT_REQUESTED="false"
SHOW_ALL="false"
SHOW_REPORTS="false"
OWNER_FILTER=""
REPORT_FILTER=""
RESOLVED_REPORT_ID=""
RESOLVED_REPORT_NAME=""
RESOLVED_REPORT_PATH=""
RESOLVED_REPORT_TYPE=""
BADGE_ERRORS="false"
VERBOSE="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --audit) AUDIT_REQUESTED="true" ;;
    --show-all)
      AUDIT_REQUESTED="true"
      SHOW_ALL="true"
      ;;
    --show-reports)
      AUDIT_REQUESTED="true"
      SHOW_REPORTS="true"
      ;;
    --dry-run) REQUESTED_MODE="dry-run" ;;
    --execute) REQUESTED_MODE="execute" ;;
    --owner)
      shift
      if [[ $# -eq 0 || -z "$1" ]]; then
        echo "ERROR: --owner requires an email, name, or member ID." >&2
        exit 2
      fi
      OWNER_FILTER="$1"
      ;;
    --owner=*)
      OWNER_FILTER="${1#--owner=}"
      if [[ -z "$OWNER_FILTER" ]]; then
        echo "ERROR: --owner requires an email, name, or member ID." >&2
        exit 2
      fi
      ;;
    --report)
      shift
      if [[ $# -eq 0 || -z "$1" ]]; then
        echo "ERROR: --report requires a report/workbook name, path, URL, or ID." >&2
        exit 2
      fi
      REPORT_FILTER="$1"
      ;;
    --report=*)
      REPORT_FILTER="${1#--report=}"
      if [[ -z "$REPORT_FILTER" ]]; then
        echo "ERROR: --report requires a report/workbook name, path, URL, or ID." >&2
        exit 2
      fi
      ;;
    --badge-errors|--flag-errors) BADGE_ERRORS="true" ;;
    --verbose) VERBOSE="true" ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "$AUDIT_REQUESTED" == "true" && -n "$REQUESTED_MODE" ]]; then
  echo "ERROR: --audit/--show-all cannot be combined with --dry-run or --execute." >&2
  exit 2
fi
if [[ "$BADGE_ERRORS" == "true" && "$REQUESTED_MODE" != "execute" ]]; then
  echo "ERROR: --badge-errors requires --execute because it changes workbook badges." >&2
  exit 2
fi
if [[ "$AUDIT_REQUESTED" == "true" ]]; then
  MODE="audit"
elif [[ -n "$REQUESTED_MODE" ]]; then
  MODE="$REQUESTED_MODE"
fi

WAIT_SECONDS="${WAIT_SECONDS:-5}"
REPORT_SCAN_CONCURRENCY="${REPORT_SCAN_CONCURRENCY:-100}"
UPDATE_REFERENCES="${UPDATE_REFERENCES:-true}"
INCLUDE_NOT_REQUIRED="${INCLUDE_NOT_REQUIRED:-false}"
SIGMA_PROFILE="${SIGMA_PROFILE:-${DE_SIGMA_PROFILE:-de}}"
LOG_FILE="${LOG_FILE:-sigma_dataset_migration_$(date +%Y%m%d_%H%M%S).jsonl}"

SIGMA_ENV="${SIGMA_ENV:-$HOME/.config/sigma/env}"
SIGMA_SKILLS_ROOT="${SIGMA_SKILLS_ROOT:-$HOME/github/sigma-skills}"
GET_TOKEN_SH="${GET_TOKEN_SH:-$SIGMA_SKILLS_ROOT/sigma-api/scripts/get-token.sh}"
BOOTSTRAP_PY="${BOOTSTRAP_PY:-$HOME/de/sigma_cli_bootstrap.py}"
export DE_SIGMA_PROFILE="$SIGMA_PROFILE"
export PATH="$HOME/.sigma-cli/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if [[ ! "$WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: WAIT_SECONDS must be a non-negative integer." >&2
  exit 2
fi
if [[ ! "$REPORT_SCAN_CONCURRENCY" =~ ^[1-9][0-9]*$ ]]; then
  echo "ERROR: REPORT_SCAN_CONCURRENCY must be a positive integer." >&2
  exit 2
fi

for setting in UPDATE_REFERENCES INCLUDE_NOT_REQUIRED; do
  value="${!setting}"
  if [[ "$value" != "true" && "$value" != "false" ]]; then
    echo "ERROR: $setting must be true or false." >&2
    exit 2
  fi
done

for dep in curl jq python3; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    echo "ERROR: '$dep' is required but not found in PATH." >&2
    exit 1
  fi
done

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
  echo "ERROR: missing Sigma env file: $SIGMA_ENV" >&2
  exit 1
fi
if [[ ! -f "$GET_TOKEN_SH" ]]; then
  echo "ERROR: missing Sigma token script: $GET_TOKEN_SH" >&2
  exit 1
fi
if [[ ! -f "$BOOTSTRAP_PY" ]]; then
  echo "ERROR: missing Sigma profile bootstrap: $BOOTSTRAP_PY" >&2
  exit 1
fi

SIGMA_BIN="$(resolve_sigma || true)"
if [[ -z "$SIGMA_BIN" ]]; then
  echo "ERROR: Sigma CLI not found." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$SIGMA_ENV"
set +a

if [[ -z "${SIGMA_BASE_URL:-}" || -z "${SIGMA_CLIENT_ID:-}" || -z "${SIGMA_CLIENT_SECRET:-}" ]]; then
  echo "ERROR: SIGMA_BASE_URL, SIGMA_CLIENT_ID, and SIGMA_CLIENT_SECRET must be set in $SIGMA_ENV" >&2
  exit 1
fi

refresh_token() {
  # The trusted local helper prints an export statement for SIGMA_API_TOKEN.
  eval "$(bash "$GET_TOKEN_SH")"
  echo "Sigma token minted (expires in about 1 hour)." >&2
}

profile_ok() {
  local output
  output="$("$SIGMA_BIN" -p "$SIGMA_PROFILE" auth status 2>&1)" || true
  [[ "$output" != *"no default profile"* && "$output" != *"error"* ]]
}

ensure_profile() {
  if profile_ok; then
    return 0
  fi
  python3 "$BOOTSTRAP_PY" "$SIGMA_BIN"
  "$SIGMA_BIN" auth set-default "$SIGMA_PROFILE" >/dev/null 2>&1 || true
  if ! profile_ok; then
    echo "ERROR: Sigma CLI profile '$SIGMA_PROFILE' could not be initialized." >&2
    return 1
  fi
}

run_sigma() {
  local code
  if "$SIGMA_BIN" -p "$SIGMA_PROFILE" "$@"; then
    return 0
  else
    code=$?
  fi

  if [[ "$code" -eq 2 ]]; then
    echo "Sigma CLI auth failed; refreshing the token and retrying once." >&2
    refresh_token
    "$SIGMA_BIN" -p "$SIGMA_PROFILE" "$@"
    return $?
  fi
  return "$code"
}

refresh_token
ensure_profile

if ! run_sigma auth status >/dev/null; then
  echo "ERROR: Sigma CLI authentication failed for profile '$SIGMA_PROFILE'." >&2
  exit 1
fi
if [[ "$MODE" != "audit" ]] && ! run_sigma api schema datasets migrate >/dev/null 2>&1; then
  echo "ERROR: the installed Sigma CLI does not expose 'datasets migrate'." >&2
  echo "Upgrade the Sigma CLI, then retry." >&2
  exit 1
fi

log_json() {
  printf '%s\n' "$1" >> "$LOG_FILE"
}

log_outcome() {
  local id="$1"
  local name="$2"
  local outcome="$3"
  local detail="${4:-}"
  local event
  event="$(jq -nc \
    --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --arg id "$id" \
    --arg name "$name" \
    --arg result "$outcome" \
    --arg detail "$detail" \
    '{timestamp:$timestamp,datasetId:$id,name:$name,result:$result} + if $detail == "" then {} else {detail:$detail} end')"
  log_json "$event"
}

SPINNER_PID=""
SCAN_TEMP_DIR=""

spinner_start() {
  local label="$1"
  local -a frames=('⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏')

  if [[ ! -t 2 ]]; then
    printf '%s...\n' "$label" >&2
    return 0
  fi

  (
    local index=0
    trap - EXIT
    trap 'exit 0' TERM INT
    while :; do
      printf '\r\033[2K%s %s' "${frames[index]}" "$label" >&2
      index=$(( (index + 1) % ${#frames[@]} ))
      sleep 0.08
    done
  ) &
  SPINNER_PID=$!
}

spinner_stop() {
  local message="$1"
  if [[ -n "$SPINNER_PID" ]]; then
    kill "$SPINNER_PID" >/dev/null 2>&1 || true
    wait "$SPINNER_PID" 2>/dev/null || true
    SPINNER_PID=""
    printf '\r\033[2K%s\n' "$message" >&2
  elif [[ ! -t 2 ]]; then
    printf '%s\n' "$message" >&2
  fi
}

cleanup_spinner() {
  if [[ -n "$SPINNER_PID" ]]; then
    kill "$SPINNER_PID" >/dev/null 2>&1 || true
    wait "$SPINNER_PID" 2>/dev/null || true
    SPINNER_PID=""
    [[ -t 2 ]] && printf '\r\033[2K' >&2
  fi
  if [[ -n "$SCAN_TEMP_DIR" && -d "$SCAN_TEMP_DIR" && "$SCAN_TEMP_DIR" == /tmp/sigma-report-scan.* ]]; then
    rm -rf -- "$SCAN_TEMP_DIR"
    SCAN_TEMP_DIR=""
  fi
}

trap cleanup_spinner EXIT
trap 'cleanup_spinner; exit 130' INT
trap 'cleanup_spinner; exit 143' TERM

fetch_all_datasets() {
  local page=""
  local all='[]'
  local response params entries next_page

  spinner_start "Loading Sigma datasets"

  while :; do
    if [[ -z "$page" ]]; then
      params='{"limit":1000,"skipPermissionCheck":true}'
    else
      params="$(jq -nc --arg page "$page" '{limit:1000,skipPermissionCheck:true,page:$page}')"
    fi

    if ! response="$(run_sigma api datasets list --params "$params")"; then
      spinner_stop "Failed to load Sigma datasets."
      echo "ERROR: failed to fetch Sigma datasets." >&2
      return 1
    fi
    if ! jq -e '
      type == "object" and
      (.entries | type == "array") and
      all(.entries[]; type == "object") and
      ((.nextPage // "") | type == "string")
    ' >/dev/null 2>&1 <<<"$response"; then
      spinner_stop "Failed to load Sigma datasets."
      echo "ERROR: Sigma datasets list returned malformed JSON." >&2
      return 1
    fi

    entries="$(jq '.entries' <<<"$response")"
    all="$(jq -nc --argjson current "$all" --argjson incoming "$entries" '$current + $incoming')"
    next_page="$(jq -r '.nextPage // empty' <<<"$response")"
    [[ -z "$next_page" ]] && break
    page="$next_page"
  done

  spinner_stop "Loaded Sigma datasets."
  printf '%s\n' "$all"
}

fetch_all_members() {
  local page=""
  local all='[]'
  local response params entries next_page

  spinner_start "Resolving dataset owners"

  while :; do
    if [[ -z "$page" ]]; then
      params='{"limit":1000,"includeArchived":true,"includeInactive":true}'
    else
      params="$(jq -nc --arg page "$page" '{limit:1000,includeArchived:true,includeInactive:true,page:$page}')"
    fi

    if ! response="$(run_sigma api members list --params "$params")"; then
      spinner_stop "Could not resolve dataset owners."
      echo "WARNING: failed to fetch Sigma members; owner IDs will be shown." >&2
      return 1
    fi
    if jq -e 'type == "array"' >/dev/null 2>&1 <<<"$response"; then
      entries="$response"
      next_page=""
    elif jq -e 'type == "object" and (.entries | type == "array")' >/dev/null 2>&1 <<<"$response"; then
      entries="$(jq '.entries' <<<"$response")"
      next_page="$(jq -r '.nextPage // empty' <<<"$response")"
    else
      spinner_stop "Could not resolve dataset owners."
      echo "WARNING: Sigma members list returned malformed JSON; owner IDs will be shown." >&2
      return 1
    fi

    all="$(jq -nc --argjson current "$all" --argjson incoming "$entries" '$current + $incoming')"
    [[ -z "$next_page" ]] && break
    page="$next_page"
  done

  spinner_stop "Resolved dataset owners."
  printf '%s\n' "$all"
}

fetch_all_documents() {
  local resource="$1"
  local id_field="$2"
  local document_type="$3"
  local page=""
  local all='[]'
  local response params entries next_page label

  label="$(printf '%s' "$document_type" | tr '[:lower:]' '[:upper:]')"
  spinner_start "Loading Sigma ${document_type}s"

  while :; do
    if [[ -z "$page" ]]; then
      if [[ "$resource" == "workbooks" ]]; then
        params='{"limit":1000,"skipPermissionCheck":true,"excludeExplorations":true}'
      else
        params='{"limit":1000,"skipPermissionCheck":true}'
      fi
    else
      if [[ "$resource" == "workbooks" ]]; then
        params="$(jq -nc --arg page "$page" '{limit:1000,skipPermissionCheck:true,excludeExplorations:true,page:$page}')"
      else
        params="$(jq -nc --arg page "$page" '{limit:1000,skipPermissionCheck:true,page:$page}')"
      fi
    fi

    if ! response="$(run_sigma api "$resource" list --params "$params")"; then
      spinner_stop "Failed to load Sigma ${document_type}s."
      echo "ERROR: failed to fetch Sigma ${document_type}s." >&2
      return 1
    fi
    if ! jq -e 'type == "object" and (.entries | type == "array")' >/dev/null 2>&1 <<<"$response"; then
      spinner_stop "Failed to load Sigma ${document_type}s."
      echo "ERROR: Sigma $resource list returned malformed JSON." >&2
      return 1
    fi

    entries="$(jq --arg id_field "$id_field" --arg document_type "$label" '
      [.entries[] | . + {
        documentId: .[$id_field],
        documentType: $document_type
      }]
    ' <<<"$response")"
    all="$(jq -nc --argjson current "$all" --argjson incoming "$entries" '$current + $incoming')"
    next_page="$(jq -r '.nextPage // empty' <<<"$response")"
    [[ -z "$next_page" ]] && break
    page="$next_page"
  done

  spinner_stop "Loaded Sigma ${document_type}s."
  printf '%s\n' "$all"
}

fetch_all_workbook_badges() {
  local page=""
  local badges='{}'
  local response params entries next_page incoming

  spinner_start "Loading Sigma workbook badges"

  while :; do
    if [[ -z "$page" ]]; then
      params='{"limit":1000,"typeFilters":"workbook"}'
    else
      params="$(jq -nc --arg page "$page" '{limit:1000,typeFilters:"workbook",page:$page}')"
    fi

    if ! response="$(run_sigma api files list --params "$params")"; then
      spinner_stop "Failed to load Sigma workbook badges."
      echo "ERROR: failed to fetch Sigma workbook badges." >&2
      return 1
    fi
    if ! jq -e '
      type == "object" and
      (.entries | type == "array") and
      ((.nextPage // "") | type == "string")
    ' >/dev/null 2>&1 <<<"$response"; then
      spinner_stop "Failed to load Sigma workbook badges."
      echo "ERROR: Sigma files list returned malformed JSON." >&2
      return 1
    fi

    entries="$(jq '.entries' <<<"$response")"
    incoming="$(jq '[.[] | select((.id // "") != "") | {
      key: .id,
      value: (.badge // null)
    }] | from_entries' <<<"$entries")"
    badges="$(jq -nc --argjson current "$badges" --argjson incoming "$incoming" '$current + $incoming')"
    next_page="$(jq -r '.nextPage // empty' <<<"$response")"
    [[ -z "$next_page" ]] && break
    page="$next_page"
  done

  spinner_stop "Loaded Sigma workbook badges."
  printf '%s\n' "$badges"
}

scan_one_document() {
  local document_file="$1"
  local target_file="$2"
  local output_file="$3"
  local document_id document_type resource response source_url

  document_id="$(jq -r '.documentId // empty' "$document_file")"
  document_type="$(jq -r '.documentType // empty' "$document_file")"
  if [[ -z "$document_id" ]]; then
    printf '%s\n' '{"dependencies":[],"error":true}' > "$output_file"
    return 0
  fi

  if [[ "$document_type" == "REPORT" ]]; then
    resource="reports"
  else
    resource="workbooks"
  fi

  source_url="${SIGMA_BASE_URL%/}/v2/$resource/$document_id/sources"
  if ! response="$(curl -fsS \
    --connect-timeout 15 \
    --max-time 120 \
    --retry 2 \
    --retry-delay 1 \
    -H "Authorization: Bearer $SIGMA_API_TOKEN" \
    -H "Accept: application/json" \
    "$source_url" 2>/dev/null)"; then
    printf '%s\n' '{"dependencies":[],"error":true}' > "$output_file"
    return 0
  fi
  response="$(jq -sc '[.[] | select(type == "array")] | last // empty' <<<"$response" 2>/dev/null || true)"
  if [[ -z "$response" ]] || ! jq -e 'type == "array"' >/dev/null 2>&1 <<<"$response"; then
    printf '%s\n' '{"dependencies":[],"error":true}' > "$output_file"
    return 0
  fi

  if ! jq -nc \
    --argjson sources "$response" \
    --slurpfile targets_file "$target_file" \
    --slurpfile document_file "$document_file" '
    ($targets_file[0]) as $targets
    | ($document_file[0]) as $document
    | {dependencies: [$sources[]
      | select(.type == "dataset")
      | .inodeId as $dataset_id
      | select($targets[$dataset_id] != null)
      | {
          documentId: $document.documentId,
          documentType: $document.documentType,
          reportName: ($document.name // "(unnamed report)"),
          reportPath: ($document.path // "-"),
          reportUrl: ($document.url // ""),
          documentBadge: ($document.documentBadge // null),
          datasetId: $dataset_id,
          datasetName: $targets[$dataset_id].datasetName,
          datasetOwner: $targets[$dataset_id].datasetOwner
        }
    ], error:false}' > "$output_file"; then
    printf '%s\n' '{"dependencies":[],"error":true}' > "$output_file"
  fi
}

scan_document_dependencies() {
  local targets_json="$1"
  local documents_json="$2"
  local target_index dependencies='[]'
  local row document_file output_file target_file pid
  local document_count failure_count=0 index=0
  local -a pids=()

  target_index="$(jq '
    map({
      key: .datasetId,
      value: {
        datasetId: .datasetId,
        datasetName: (.name // "(unnamed dataset)"),
        datasetOwner: (.ownerDisplay // .owner // "(unassigned)")
      }
    }) | from_entries
  ' <<<"$targets_json")"
  document_count="$(jq 'length' <<<"$documents_json")"
  SCAN_TEMP_DIR="$(mktemp -d /tmp/sigma-report-scan.XXXXXX)"
  trap cleanup_spinner EXIT
  trap 'cleanup_spinner; exit 130' INT
  trap 'cleanup_spinner; exit 143' TERM
  target_file="$SCAN_TEMP_DIR/targets.json"
  printf '%s\n' "$target_index" > "$target_file"
  spinner_start "Scanning $document_count reports/workbooks (${REPORT_SCAN_CONCURRENCY} parallel)"

  while IFS= read -r row; do
    index=$((index + 1))
    document_file="$SCAN_TEMP_DIR/$(printf '%05d' "$index").input.json"
    output_file="$SCAN_TEMP_DIR/$(printf '%05d' "$index").result.json"
    printf '%s\n' "$row" > "$document_file"
    (
      trap - EXIT INT TERM
      scan_one_document "$document_file" "$target_file" "$output_file"
    ) &
    pids+=("$!")

    if [[ "${#pids[@]}" -ge "$REPORT_SCAN_CONCURRENCY" ]]; then
      wait "${pids[0]}" || true
      pids=("${pids[@]:1}")
    fi
  done < <(jq -c '.[]' <<<"$documents_json")

  for pid in "${pids[@]}"; do
    wait "$pid" || true
  done

  spinner_stop "Scanned $document_count reports/workbooks."
  if [[ "$document_count" -gt 0 ]]; then
    dependencies="$(jq -s '[.[].dependencies[]]' "$SCAN_TEMP_DIR"/*.result.json)"
    failure_count="$(jq -s '[.[] | select(.error == true)] | length' "$SCAN_TEMP_DIR"/*.result.json)"
  fi
  rm -rf -- "$SCAN_TEMP_DIR"
  SCAN_TEMP_DIR=""
  jq -nc \
    --argjson dependencies "$dependencies" \
    --argjson failures "$failure_count" \
    '{dependencies:($dependencies | unique_by([.documentId, .datasetId])),failures:$failures}'
}

resolve_report_document() {
  local documents_json="$1"
  local selector="$2"
  local matches count

  matches="$(jq -c --arg selector "$selector" '
    ($selector | ascii_downcase) as $needle
    | [ .[] | select(
        [(.documentId // ""), (.name // ""), (.path // ""), (.url // "")]
        | any((ascii_downcase == $needle))
      ) ]
  ' <<<"$documents_json")"
  count="$(jq 'length' <<<"$matches")"

  if [[ "$count" -eq 0 ]]; then
    matches="$(jq -c --arg selector "$selector" '
      ($selector | ascii_downcase) as $needle
      | [ .[] | select(
          [(.documentId // ""), (.name // ""), (.path // ""), (.url // "")]
          | any((ascii_downcase | contains($needle)))
        ) ]
    ' <<<"$documents_json")"
    count="$(jq 'length' <<<"$matches")"
  fi

  if [[ "$count" -eq 0 ]]; then
    echo "ERROR: report/workbook '$selector' was not found." >&2
    return 1
  fi
  if [[ "$count" -gt 1 ]]; then
    echo "ERROR: report filter '$selector' matched multiple reports/workbooks:" >&2
    jq -r 'sort_by(.documentType, .path, .name)[]
      | "  - \(.documentType): \(.name // "(unnamed)") | \(.path // "-") | \(.documentId)"' \
      <<<"$matches" >&2
    echo "Use the exact path, URL, or document ID." >&2
    return 2
  fi

  jq -c '.[0]' <<<"$matches"
}

filter_targets_by_report() {
  local targets_json="$1"
  local document_json="$2"
  local report_scan dependencies failures

  report_scan="$(scan_document_dependencies "$targets_json" "[$document_json]")"
  if ! jq -e 'type == "object" and (.dependencies | type == "array") and (.failures | type == "number")' \
    >/dev/null 2>&1 <<<"$report_scan"; then
    echo "ERROR: Sigma returned malformed report dependency data." >&2
    return 1
  fi
  failures="$(jq -r '.failures' <<<"$report_scan")"
  if [[ "$failures" -gt 0 ]]; then
    echo "ERROR: failed to read dataset sources for the selected report/workbook." >&2
    return 1
  fi

  dependencies="$(jq '.dependencies' <<<"$report_scan")"
  jq -enc --argjson targets "$targets_json" --argjson dependencies "$dependencies" '
    [
      $targets[]
      | . as $target
      | select($dependencies | any(.datasetId == $target.datasetId))
      | $target
    ]
    | select(type == "array" and all(.[]; type == "object"))
  '
}

resolve_target_owners() {
  local targets_json="$1"
  local members_json="$2"
  jq -nc --argjson targets "$targets_json" --argjson members "$members_json" '
    ($members
      | map({
          key: .memberId,
          value: {
            display: (
              if (.email // "") != "" then .email
              elif (((.firstName // "") + " " + (.lastName // "")) | gsub("^ +| +$"; "")) != ""
                then ((.firstName // "") + " " + (.lastName // "") | gsub("^ +| +$"; ""))
              else .memberId
              end
            ),
            name: ((.firstName // "") + " " + (.lastName // "") | gsub("^ +| +$"; ""))
          }
        })
      | from_entries) as $owners
    | $targets
    | map(. + {
        ownerDisplay: ($owners[.owner].display // .owner // "(unassigned)"),
        ownerName: ($owners[.owner].name // "")
      })
  '
}

filter_targets_by_owner() {
  local targets_json="$1"
  local owner_filter="$2"
  jq -nc --argjson targets "$targets_json" --arg owner_filter "$owner_filter" '
    ($owner_filter | ascii_downcase) as $needle
    | $targets
    | map(select(
        [(.owner // ""), (.ownerDisplay // ""), (.ownerName // "")]
        | any(. | ascii_downcase | contains($needle))
      ))
  '
}

truncate_field() {
  local value="$1"
  local width="$2"
  if (( ${#value} <= width )); then
    printf '%s' "$value"
  else
    printf '%s...' "${value:0:width-3}"
  fi
}

format_duration() {
  local total_seconds="$1"
  local hours=$((total_seconds / 3600))
  local minutes=$(((total_seconds % 3600) / 60))
  local seconds=$((total_seconds % 60))
  if (( hours > 0 )); then
    printf '%dh %02dm %02ds' "$hours" "$minutes" "$seconds"
  else
    printf '%dm %02ds' "$minutes" "$seconds"
  fi
}

print_audit_report() {
  local datasets_json="$1"
  local targets_json="$2"
  local dependencies_json="${3:-[]}"
  local total_count migrated_count not_migrated_count not_required_count
  local target_count owner_count reference_count target_percent generated_at
  local deprecated_dashboard_count warning_dashboard_count endorsed_dashboard_count
  local owner count refs index owner_display name_display path_display

  total_count="$(jq 'length' <<<"$datasets_json")"
  migrated_count="$(jq '[.[] | select(.migrationStatus == "migrated")] | length' <<<"$datasets_json")"
  not_migrated_count="$(jq '[.[] | select(.migrationStatus == "not-migrated")] | length' <<<"$datasets_json")"
  not_required_count="$(jq '[.[] | select(.migrationStatus == "not-required")] | length' <<<"$datasets_json")"
  target_count="$(jq 'length' <<<"$targets_json")"
  owner_count="$(jq '[.[] | (.ownerDisplay // .owner // "(unassigned)")] | unique | length' <<<"$targets_json")"
  reference_count="$(jq '[.[] | (.referenceCount // 0)] | add // 0' <<<"$targets_json")"
  deprecated_dashboard_count="$(jq '[.[]
    | select(.documentType == "WORKBOOK")
    | select((.documentBadge // "" | ascii_downcase) == "deprecated")]
    | unique_by(.documentId) | length' <<<"$dependencies_json")"
  warning_dashboard_count="$(jq '[.[]
    | select(.documentType == "WORKBOOK")
    | select((.documentBadge // "" | ascii_downcase) == "warning")]
    | unique_by(.documentId) | length' <<<"$dependencies_json")"
  endorsed_dashboard_count="$(jq '[.[]
    | select(.documentType == "WORKBOOK")
    | select((.documentBadge // "" | ascii_downcase) == "endorsed")]
    | unique_by(.documentId) | length' <<<"$dependencies_json")"
  target_percent="$(jq -nr --argjson targets "$target_count" --argjson total "$total_count" \
    'if $total == 0 then "0.0" else (($targets * 10000 / $total | round) / 100 | tostring) end')"
  generated_at="$(date '+%Y-%m-%d %H:%M:%S %Z')"

  echo
  echo "+------------------------------------------------------------------------------+"
  echo "|                    SIGMA DATASET MIGRATION AUDIT                             |"
  echo "+------------------------------------------------------------------------------+"
  printf '| Generated: %-66s |\n' "$generated_at"
  printf '| Profile:   %-66s |\n' "$SIGMA_PROFILE"
  if [[ -n "$OWNER_FILTER" ]]; then
    printf '| Owner:     %-66s |\n' "$(truncate_field "$OWNER_FILTER" 66)"
  fi
  if [[ -n "$RESOLVED_REPORT_NAME" ]]; then
    printf '| Report:    %-66s |\n' "$(truncate_field "$RESOLVED_REPORT_NAME" 66)"
  fi
  echo "+--------------------------------------+---------------------------------------+"
  printf '| %-36s | %37s |\n' "Total datasets" "$total_count"
  printf '| %-36s | %37s |\n' "Need migration" "$not_migrated_count"
  printf '| %-36s | %37s |\n' "Selected migration targets" "$target_count"
  printf '| %-36s | %36s%% |\n' "Migration queue" "$target_percent"
  printf '| %-36s | %37s |\n' "Already migrated" "$migrated_count"
  printf '| %-36s | %37s |\n' "Migration not required" "$not_required_count"
  printf '| %-36s | %37s |\n' "References affected" "$reference_count"
  printf '| %-36s | %37s |\n' "Owners with datasets in queue" "$owner_count"
  printf '| %-36s | %37s |\n' "Deprecated dashboards with refs" "$deprecated_dashboard_count"
  printf '| %-36s | %37s |\n' "Warning dashboards with refs" "$warning_dashboard_count"
  printf '| %-36s | %37s |\n' "Endorsed dashboards with refs" "$endorsed_dashboard_count"
  echo "+--------------------------------------+---------------------------------------+"

  echo
  echo "MIGRATION QUEUE BY OWNER"
  echo "+------------------------------------------+----------+------------+"
  printf '| %-40s | %8s | %10s |\n' "Owner" "Datasets" "References"
  echo "+------------------------------------------+----------+------------+"
  while IFS=$'\t' read -r owner count refs; do
    owner_display="$(truncate_field "$owner" 40)"
    printf '| %-40s | %8s | %10s |\n' "$owner_display" "$count" "$refs"
  done < <(jq -r '
    group_by(.ownerDisplay // .owner // "(unassigned)")
    | map({
        owner: (.[0].ownerDisplay // .[0].owner // "(unassigned)"),
        count: length,
        references: ([.[] | (.referenceCount // 0)] | add // 0)
      })
    | sort_by(-.count, .owner)
    | .[]
    | [.owner, .count, .references]
    | @tsv
  ' <<<"$targets_json")
  echo "+------------------------------------------+----------+------------+"

  if [[ "$SHOW_ALL" != "true" ]]; then
    echo
    echo "Run with --audit --show-all to list every dataset and owner."
    return 0
  fi

  echo
  echo "DATASETS NEEDING MIGRATION"
  echo "+-----+----------------------------------+------+------------------------------------------+--------------------------+"
  printf '| %-3s | %-32s | %-4s | %-40s | %-24s |\n' "#" "Owner" "Refs" "Dataset" "Path"
  echo "+-----+----------------------------------+------+------------------------------------------+--------------------------+"
  index=0
  while IFS=$'\t' read -r owner refs name path; do
    index=$((index + 1))
    owner_display="$(truncate_field "$owner" 32)"
    name_display="$(truncate_field "$name" 40)"
    path_display="$(truncate_field "$path" 24)"
    printf '| %3d | %-32s | %4s | %-40s | %-24s |\n' \
      "$index" "$owner_display" "$refs" "$name_display" "$path_display"
  done < <(jq -r '
    sort_by((.ownerDisplay // .owner // "(unassigned)"), (.name // ""))
    | .[]
    | [
        (.ownerDisplay // .owner // "(unassigned)"),
        (.referenceCount // 0),
        (.name // "(unnamed dataset)"),
        (.path // "-")
      ]
    | @tsv
  ' <<<"$targets_json")
  echo "+-----+----------------------------------+------+------------------------------------------+--------------------------+"
  printf 'Listed %d dataset(s).\n' "$index"
}

print_report_dependencies() {
  local dependencies_json="$1"
  local report_count dependency_count index=0
  local type report location dataset
  local report_display location_display dataset_display

  report_count="$(jq '[.[].documentId] | unique | length' <<<"$dependencies_json")"
  dependency_count="$(jq 'length' <<<"$dependencies_json")"

  echo
  echo "REPORTS/WORKBOOKS USING DATASETS IN THE MIGRATION QUEUE"
  printf 'Affected reports/workbooks: %d | Dataset references: %d\n' "$report_count" "$dependency_count"
  echo "+-----+----------+----------------------------------+------------------------------+----------------------------------+"
  printf '| %-3s | %-8s | %-32s | %-28s | %-32s |\n' "#" "Type" "Report" "Location" "Dataset"
  echo "+-----+----------+----------------------------------+------------------------------+----------------------------------+"
  while IFS=$'\t' read -r type report location dataset; do
    index=$((index + 1))
    report_display="$(truncate_field "$report" 32)"
    location_display="$(truncate_field "$location" 28)"
    dataset_display="$(truncate_field "$dataset" 32)"
    printf '| %3d | %-8s | %-32s | %-28s | %-32s |\n' \
      "$index" "$type" "$report_display" "$location_display" "$dataset_display"
  done < <(jq -r '
    sort_by(.documentType, .reportPath, .reportName, .datasetName)
    | .[]
    | [.documentType, .reportName, .reportPath, .datasetName]
    | @tsv
  ' <<<"$dependencies_json")
  echo "+-----+----------+----------------------------------+------------------------------+----------------------------------+"
  if [[ "$dependency_count" -eq 0 ]]; then
    echo "No direct report/workbook references found for the selected datasets."
  fi
}

get_dataset_status() {
  local id="$1"
  local params response
  params="$(jq -nc --arg id "$id" '{datasetId:$id}')"
  if ! response="$(run_sigma api datasets get --params "$params")"; then
    return 1
  fi
  jq -er '.migrationStatus | select(type == "string" and length > 0)' <<<"$response"
}

# Set by migrate_one for precise live counters.
# Values: migrated, dry-run-ok, skipped-already-migrated,
# skipped-not-required, failed-api, failed-verification.
MIGRATION_OUTCOME=""
REPORT_DEPENDENCIES='[]'
REPORT_SCAN_ATTEMPTED="false"
REPORT_SCAN_FAILURES=0

load_error_report_dependencies() {
  local workbooks reports documents report_scan

  if [[ "$REPORT_SCAN_ATTEMPTED" == "true" ]]; then
    [[ "$REPORT_SCAN_FAILURES" -ge 0 ]]
    return
  fi
  REPORT_SCAN_ATTEMPTED="true"

  echo "    Resolving affected dashboards/reports..." >&2
  if ! workbooks="$(fetch_all_documents "workbooks" "workbookId" "workbook")"; then
    REPORT_SCAN_FAILURES=-1
    return 1
  fi
  if ! reports="$(fetch_all_documents "reports" "reportId" "report")"; then
    REPORT_SCAN_FAILURES=-1
    return 1
  fi

  documents="$(jq -nc --argjson workbooks "$workbooks" --argjson reports "$reports" '$workbooks + $reports')"
  if ! report_scan="$(scan_document_dependencies "$targets" "$documents")"; then
    REPORT_SCAN_FAILURES=-1
    return 1
  fi
  if ! jq -e 'type == "object" and (.dependencies | type == "array") and (.failures | type == "number")' \
    >/dev/null 2>&1 <<<"$report_scan"; then
    REPORT_SCAN_FAILURES=-1
    return 1
  fi

  REPORT_DEPENDENCIES="$(jq '.dependencies' <<<"$report_scan")"
  REPORT_SCAN_FAILURES="$(jq -r '.failures' <<<"$report_scan")"
}

print_error_report_dependencies() {
  local dataset_id="$1"
  local matches count type name location url

  if ! load_error_report_dependencies; then
    echo "    Affected dashboards/reports: unavailable (dependency scan failed)" >&2
    return 0
  fi

  matches="$(jq -c --arg dataset_id "$dataset_id" '
    [.[] | select(.datasetId == $dataset_id)]
    | unique_by([.documentId, .datasetId])
    | sort_by(.documentType, .reportPath, .reportName)
  ' <<<"$REPORT_DEPENDENCIES")"
  count="$(jq 'length' <<<"$matches")"

  if [[ "$count" -eq 0 ]]; then
    if [[ "$REPORT_SCAN_FAILURES" -gt 0 ]]; then
      printf '    Affected dashboards/reports: none found (scan incomplete: %d request(s) failed)\n' \
        "$REPORT_SCAN_FAILURES" >&2
    else
      echo "    Affected dashboards/reports: none found" >&2
    fi
    return 0
  fi

  echo "    Affected dashboards/reports:" >&2
  while IFS=$'\t' read -r type name location url; do
    printf '      - %s: %s\n' "$type" "$name" >&2
    printf '        Location: %s\n' "$location" >&2
    [[ -n "$url" ]] && printf '        URL: %s\n' "$url" >&2
  done < <(jq -r '.[] | [
    (.documentType // "REPORT"),
    (.reportName // "(unnamed report)"),
    (.reportPath // "-"),
    (.reportUrl // "")
  ] | @tsv' <<<"$matches")

  if [[ "$REPORT_SCAN_FAILURES" -gt 0 ]]; then
    printf '    Note: %d dependency request(s) failed; this list may be incomplete.\n' \
      "$REPORT_SCAN_FAILURES" >&2
  fi
}

get_workbook_badge() {
  local workbook_id="$1"
  local params response

  params="$(jq -nc --arg id "$workbook_id" '{inodeId:$id}')"
  if ! response="$(run_sigma api files get --params "$params")"; then
    return 1
  fi
  jq -er '.badge // "None"' <<<"$response"
}

manage_error_workbook_badges() {
  local pending selected workbook_id workbook_name workbook_path datasets workbook_url
  local current_badge badge event remaining skipped_reports confirmation

  if [[ "$BADGE_ERRORS" != "true" || "$failed_count" -eq 0 ]]; then
    return 0
  fi
  if ! command -v fzf >/dev/null 2>&1; then
    echo "WARNING: fzf is required for --badge-errors; no workbook badges were changed." >&2
    return 0
  fi
  if [[ ! -t 0 || ! -t 1 ]]; then
    echo "WARNING: --badge-errors requires an interactive terminal; no workbook badges were changed." >&2
    return 0
  fi

  pending="$(jq -nc \
    --argjson dependencies "$REPORT_DEPENDENCIES" \
    --argjson failed_ids "$FAILED_DATASET_IDS" '
    [
      $dependencies[]
      | select(.documentType == "WORKBOOK")
      | select(.datasetId as $id | $failed_ids | index($id))
    ]
    | group_by(.documentId)
    | map({
        documentId: .[0].documentId,
        reportName: .[0].reportName,
        reportPath: .[0].reportPath,
        reportUrl: .[0].reportUrl,
        datasets: ([.[].datasetName] | unique | sort)
      })
    | sort_by(.reportPath, .reportName)
  ')"
  skipped_reports="$(jq -n \
    --argjson dependencies "$REPORT_DEPENDENCIES" \
    --argjson failed_ids "$FAILED_DATASET_IDS" '
    [$dependencies[]
      | select(.documentType != "WORKBOOK")
      | select(.datasetId as $id | $failed_ids | index($id))
      | .documentId] | unique | length
  ')"
  remaining="$(jq 'length' <<<"$pending")"

  echo
  echo "============================================================"
  echo "Failed workbook badge review"
  echo "============================================================"
  if [[ "$remaining" -eq 0 ]]; then
    echo "No affected workbooks were found for the failed datasets."
    [[ "$REPORT_SCAN_FAILURES" -gt 0 ]] && \
      echo "Note: $REPORT_SCAN_FAILURES dependency request(s) failed; results may be incomplete."
    return 0
  fi

  printf 'Workbooks available for review: %d\n' "$remaining"
  [[ "$skipped_reports" -gt 0 ]] && \
    printf 'Reports excluded (Sigma document badges apply to workbooks/data models): %d\n' "$skipped_reports"
  echo "Select a workbook, then choose Warning, Deprecated, or Skip. Press Esc to finish."
  echo "Sigma's public API does not support badge writes; the workbook opens for the supported UI change."

  while [[ "$(jq 'length' <<<"$pending")" -gt 0 ]]; do
    if ! selected="$(jq -r '.[] | [
        .documentId,
        .reportName,
        .reportPath,
        (.datasets | join(", ")),
        .reportUrl
      ] | @tsv' <<<"$pending" \
      | fzf --height=80% --border --delimiter=$'\t' --with-nth=2,3,4 \
          --prompt='Workbook to badge > ' \
          --header='NAME | LOCATION | FAILED DATASETS   (Esc: finish)')"; then
      echo "Badge review finished."
      break
    fi

    IFS=$'\t' read -r workbook_id workbook_name workbook_path datasets workbook_url <<<"$selected"
    current_badge="$(get_workbook_badge "$workbook_id" 2>/dev/null || printf 'Unknown')"
    echo
    echo "Workbook:      $workbook_name"
    echo "Location:      $workbook_path"
    echo "Failed source: $datasets"
    echo "Current badge: $current_badge"
    [[ -n "$workbook_url" ]] && echo "URL:           $workbook_url"

    if ! badge="$(printf '%s\n' 'Warning' 'Deprecated' 'Skip' \
      | fzf --height=40% --border --prompt='Set badge > ' \
          --header="$workbook_name (Esc: return to workbook list)")"; then
      continue
    fi

    pending="$(jq -c --arg id "$workbook_id" 'map(select(.documentId != $id))' <<<"$pending")"
    if [[ "$badge" == "Skip" ]]; then
      echo "Skipped: $workbook_name"
      BADGE_SKIPPED_COUNT=$((BADGE_SKIPPED_COUNT + 1))
      continue
    fi

    if [[ -n "$workbook_url" && "$(uname -s)" == "Darwin" ]] && command -v open >/dev/null 2>&1; then
      if open "$workbook_url" >/dev/null 2>&1; then
        echo "Opened workbook in your browser."
      else
        echo "WARNING: could not open the workbook automatically; use the URL above." >&2
      fi
    else
      echo "Open the URL above in Sigma."
    fi
    echo "In Sigma, choose File > Set badge > $badge, then apply the change."

    while :; do
      printf 'Press Enter to verify, or type s to skip: '
      IFS= read -r confirmation
      if [[ "$confirmation" == "s" || "$confirmation" == "S" ]]; then
        echo "Skipped verification: $workbook_name"
        BADGE_SKIPPED_COUNT=$((BADGE_SKIPPED_COUNT + 1))
        break
      fi
      if ! current_badge="$(get_workbook_badge "$workbook_id" 2>/dev/null)"; then
        echo "WARNING: could not retrieve the current badge; retry or type s to skip." >&2
        continue
      fi
      if [[ "$current_badge" != "$badge" ]]; then
        echo "Badge is still '$current_badge'; apply '$badge' in Sigma, then verify again."
        continue
      fi

      echo "Badge verified: $workbook_name -> $badge"
      BADGE_UPDATED_COUNT=$((BADGE_UPDATED_COUNT + 1))
      event="$(jq -nc \
        --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --arg workbook_id "$workbook_id" \
        --arg workbook_name "$workbook_name" \
        --arg badge "$badge" \
        '{timestamp:$timestamp,event:"workbook-badge-verified",workbookId:$workbook_id,workbookName:$workbook_name,badge:$badge}')"
      log_json "$event"
      break
    done
  done

  echo
  echo "Badge review summary"
  echo "Verified: $BADGE_UPDATED_COUNT | Skipped: $BADGE_SKIPPED_COUNT | Failed: $BADGE_FAILED_COUNT"
}

migrate_one() {
  local id="$1"
  local name="$2"
  local current_status="$3"
  local params body result status_after data_model_id links references
  local dry_run_json="false"

  MIGRATION_OUTCOME=""
  [[ "$MODE" == "dry-run" ]] && dry_run_json="true"
  params="$(jq -nc --arg id "$id" '{datasetId:$id}')"
  body="$(jq -nc \
    --argjson update_references "$UPDATE_REFERENCES" \
    --argjson dry_run "$dry_run_json" \
    '{shouldUpdateReferences:$update_references,dryRun:$dry_run}')"

  if [[ "$VERBOSE" == "true" ]]; then
    echo
    echo "==> $name"
    echo "    datasetId: $id"
    echo "    current:   $current_status"
  fi

  if [[ "$MODE" == "execute" ]]; then
    if ! current_status="$(get_dataset_status "$id")"; then
      echo "ERROR: $name ($id): could not re-check dataset status" >&2
      print_error_report_dependencies "$id"
      MIGRATION_OUTCOME="failed-api"
      log_outcome "$id" "$name" "$MIGRATION_OUTCOME" "status-recheck"
      return 1
    fi
    if [[ "$current_status" == "migrated" ]]; then
      [[ "$VERBOSE" == "true" ]] && echo "    SKIP: already migrated"
      MIGRATION_OUTCOME="skipped-already-migrated"
      log_outcome "$id" "$name" "$MIGRATION_OUTCOME"
      return 0
    fi
    if [[ "$current_status" == "not-required" && "$INCLUDE_NOT_REQUIRED" != "true" ]]; then
      [[ "$VERBOSE" == "true" ]] && echo "    SKIP: now marked not-required"
      MIGRATION_OUTCOME="skipped-not-required"
      log_outcome "$id" "$name" "$MIGRATION_OUTCOME"
      return 0
    fi
  fi

  if ! result="$(run_sigma api datasets migrate --params "$params" --json "$body")"; then
    echo "ERROR: $name ($id): migration API call failed" >&2
    print_error_report_dependencies "$id"
    MIGRATION_OUTCOME="failed-api"
    log_outcome "$id" "$name" "$MIGRATION_OUTCOME" "migration-call"
    return 1
  fi
  if ! jq -e 'type == "object"' >/dev/null 2>&1 <<<"$result"; then
    echo "ERROR: $name ($id): migration API returned malformed JSON" >&2
    print_error_report_dependencies "$id"
    MIGRATION_OUTCOME="failed-api"
    log_outcome "$id" "$name" "$MIGRATION_OUTCOME" "malformed-response"
    return 1
  fi

  if [[ "$MODE" == "dry-run" ]]; then
    links="$(jq -r '.linkCount // 0' <<<"$result")"
    references="$(jq -r '.referenceCount // 0' <<<"$result")"
    [[ "$VERBOSE" == "true" ]] && echo "    DRY RUN OK | links=$links references=$references"
    MIGRATION_OUTCOME="dry-run-ok"
    log_outcome "$id" "$name" "$MIGRATION_OUTCOME" "links=$links references=$references"
    return 0
  fi

  data_model_id="$(jq -r '.dataModelId // empty' <<<"$result")"
  [[ "$VERBOSE" == "true" ]] && echo "    API OK | dataModelId=${data_model_id:-unknown}"
  sleep 2
  if ! status_after="$(get_dataset_status "$id")"; then
    echo "ERROR: $name ($id): migration succeeded but verification request failed" >&2
    print_error_report_dependencies "$id"
    MIGRATION_OUTCOME="failed-verification"
    log_outcome "$id" "$name" "$MIGRATION_OUTCOME" "verification-request"
    return 1
  fi
  [[ "$VERBOSE" == "true" ]] && echo "    verified: $status_after"

  if [[ "$status_after" == "migrated" ]]; then
    MIGRATION_OUTCOME="migrated"
    log_outcome "$id" "$name" "$MIGRATION_OUTCOME" "dataModelId=${data_model_id:-unknown}"
    return 0
  fi

  echo "WARNING: $name ($id): API returned success but status is '$status_after'" >&2
  print_error_report_dependencies "$id"
  MIGRATION_OUTCOME="failed-verification"
  log_outcome "$id" "$name" "$MIGRATION_OUTCOME" "statusAfter=$status_after"
  return 1
}

if ! datasets="$(fetch_all_datasets)"; then
  if [[ "$MODE" != "audit" ]]; then
    log_json "$(jq -nc --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '{timestamp:$timestamp,event:"run-failed",detail:"initial-inventory"}')"
  fi
  exit 1
fi

if [[ "$INCLUDE_NOT_REQUIRED" == "true" ]]; then
  targets="$(jq '[.[] | select(.migrationStatus == "not-migrated" or .migrationStatus == "not-required")]' <<<"$datasets")"
else
  targets="$(jq '[.[] | select(.migrationStatus == "not-migrated")]' <<<"$datasets")"
fi

total="$(jq 'length' <<<"$datasets")"
initial_migrated="$(jq '[.[] | select(.migrationStatus == "migrated")] | length' <<<"$datasets")"

if [[ "$MODE" == "audit" || -n "$OWNER_FILTER" ]]; then
  members='[]'
  if fetched_members="$(fetch_all_members)"; then
    members="$fetched_members"
  elif [[ -n "$OWNER_FILTER" ]]; then
    echo "ERROR: owner filtering requires the Sigma members inventory." >&2
    exit 1
  fi
  targets="$(resolve_target_owners "$targets" "$members")"
fi

if [[ -n "$OWNER_FILTER" ]]; then
  targets="$(filter_targets_by_owner "$targets" "$OWNER_FILTER")"
  matched_owner_count="$(jq '[.[] | (.ownerDisplay // .owner)] | unique | length' <<<"$targets")"
  if [[ "$matched_owner_count" -gt 1 ]]; then
    echo "ERROR: owner filter '$OWNER_FILTER' matched multiple owners:" >&2
    jq -r '[.[] | (.ownerDisplay // .owner)] | unique[] | "  - \(.)"' <<<"$targets" >&2
    echo "Use a full email address, full name, or member ID." >&2
    exit 2
  fi
fi

if [[ -n "$REPORT_FILTER" ]]; then
  workbooks="$(fetch_all_documents "workbooks" "workbookId" "workbook")" || exit 1
  reports="$(fetch_all_documents "reports" "reportId" "report")" || exit 1
  documents="$(jq -nc --argjson workbooks "$workbooks" --argjson reports "$reports" '$workbooks + $reports')"
  selected_report="$(resolve_report_document "$documents" "$REPORT_FILTER")" || exit $?

  RESOLVED_REPORT_ID="$(jq -r '.documentId' <<<"$selected_report")"
  RESOLVED_REPORT_NAME="$(jq -r '.name // "(unnamed report)"' <<<"$selected_report")"
  RESOLVED_REPORT_PATH="$(jq -r '.path // "-"' <<<"$selected_report")"
  RESOLVED_REPORT_TYPE="$(jq -r '.documentType // "REPORT"' <<<"$selected_report")"
  targets="$(filter_targets_by_report "$targets" "$selected_report")" || exit 1
fi

target_count="$(jq 'length' <<<"$targets")"

if [[ "$MODE" == "audit" ]]; then
  report_scan_failures=0
  report_dependencies='[]'
  if [[ "$target_count" -gt 0 ]]; then
    workbooks="$(fetch_all_documents "workbooks" "workbookId" "workbook")" || exit 1
    workbook_badges="$(fetch_all_workbook_badges)" || exit 1
    workbooks="$(jq -nc --argjson workbooks "$workbooks" --argjson badges "$workbook_badges" '
      $workbooks | map(. + {documentBadge: ($badges[.documentId] // null)})
    ')"
    documents="$workbooks"
    if [[ "$SHOW_REPORTS" == "true" ]]; then
      reports="$(fetch_all_documents "reports" "reportId" "report")" || exit 1
      documents="$(jq -nc --argjson workbooks "$workbooks" --argjson reports "$reports" '$workbooks + $reports')"
    fi
    report_scan="$(scan_document_dependencies "$targets" "$documents")"
    report_dependencies="$(jq '.dependencies' <<<"$report_scan")"
    report_scan_failures="$(jq '.failures' <<<"$report_scan")"
  fi
  print_audit_report "$datasets" "$targets" "$report_dependencies"
  if [[ "$SHOW_REPORTS" == "true" ]]; then
    print_report_dependencies "$report_dependencies"
  fi
  if [[ "$report_scan_failures" -gt 0 ]]; then
    echo "WARNING: $report_scan_failures report/workbook source request(s) failed; results may be incomplete." >&2
  fi
  exit 0
fi

if ! touch "$LOG_FILE"; then
  echo "ERROR: cannot write audit log: $LOG_FILE" >&2
  exit 1
fi

echo "Sigma dataset migration"
echo "Mode:                 $MODE"
echo "Profile:              $SIGMA_PROFILE"
echo "Update references:    $UPDATE_REFERENCES"
echo "Include not-required: $INCLUDE_NOT_REQUIRED"
[[ -n "$OWNER_FILTER" ]] && echo "Owner filter:         $OWNER_FILTER"
if [[ -n "$REPORT_FILTER" ]]; then
  echo "Report filter:        $REPORT_FILTER"
  echo "Resolved report:      $RESOLVED_REPORT_TYPE: $RESOLVED_REPORT_NAME"
  echo "Report location:      $RESOLVED_REPORT_PATH"
fi
echo "Verbose:              $VERBOSE"
echo "Wait between items:   ${WAIT_SECONDS}s"
echo "Log:                  $LOG_FILE"

log_json "$(jq -nc \
  --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg mode "$MODE" \
  --arg profile "$SIGMA_PROFILE" \
  --arg report_filter "$REPORT_FILTER" \
  --arg report_id "$RESOLVED_REPORT_ID" \
  '{timestamp:$timestamp,event:"run-start",mode:$mode,profile:$profile,reportFilter:$report_filter,reportId:$report_id}')"

printf 'Found %s datasets total; %s target(s); %s already migrated.\n' \
  "$total" "$target_count" "$initial_migrated"

if [[ -n "$OWNER_FILTER" && "$target_count" -eq 0 ]]; then
  echo "No eligible datasets matched owner '$OWNER_FILTER'."
fi
if [[ -n "$REPORT_FILTER" && "$target_count" -eq 0 ]]; then
  echo "No eligible datasets matched report/workbook '$RESOLVED_REPORT_NAME'."
fi

if [[ "$MODE" == "execute" && "$target_count" -gt 0 ]]; then
  echo
  echo "EXECUTION MODE: migrations will be performed."
  echo "Press Ctrl-C within 5 seconds to abort..."
  sleep 5
fi

migration_started_epoch="$(date +%s)"

processed=0
migrated_count=0
dry_run_ok_count=0
skipped_count=0
failed_count=0
FAILED_DATASET_IDS='[]'
BADGE_UPDATED_COUNT=0
BADGE_SKIPPED_COUNT=0
BADGE_FAILED_COUNT=0
index=0

print_progress() {
  local message
  if [[ "$MODE" == "execute" ]]; then
    printf -v message 'Progress %d/%d | migrated: %d | skipped: %d | failed: %d' \
      "$processed" "$target_count" "$migrated_count" "$skipped_count" "$failed_count"
  else
    printf -v message 'Progress %d/%d | dry-run OK: %d | skipped: %d | failed: %d' \
      "$processed" "$target_count" "$dry_run_ok_count" "$skipped_count" "$failed_count"
  fi

  if [[ "$VERBOSE" == "true" ]]; then
    printf '    %s\n' "$message"
  elif [[ -t 1 ]]; then
    printf '\r\033[2K%s' "$message"
    if [[ "$processed" -eq "$target_count" ]]; then
      printf '\n'
    fi
  elif (( processed == target_count || processed % 10 == 0 )); then
    printf '%s\n' "$message"
  fi
}

while IFS= read -r row; do
  index=$((index + 1))
  id="$(jq -r '.datasetId // empty' <<<"$row")"
  name="$(jq -r '.name // "(unnamed dataset)"' <<<"$row")"
  status="$(jq -r '.migrationStatus // "unknown"' <<<"$row")"

  if [[ "$VERBOSE" != "true" && -t 1 && "$processed" -gt 0 ]]; then
    printf '\r\033[2K'
  fi
  [[ "$VERBOSE" == "true" ]] && echo "[$index/$target_count]"
  if [[ -z "$id" ]]; then
    echo "ERROR: $name: target is missing datasetId" >&2
    MIGRATION_OUTCOME="failed-api"
    log_outcome "" "$name" "$MIGRATION_OUTCOME" "missing-dataset-id"
  else
    migrate_one "$id" "$name" "$status" || true
  fi

  processed=$((processed + 1))
  case "$MIGRATION_OUTCOME" in
    migrated) migrated_count=$((migrated_count + 1)) ;;
    dry-run-ok) dry_run_ok_count=$((dry_run_ok_count + 1)) ;;
    skipped-already-migrated|skipped-not-required) skipped_count=$((skipped_count + 1)) ;;
    failed-api|failed-verification|"") failed_count=$((failed_count + 1)) ;;
  esac
  if [[ "$MIGRATION_OUTCOME" == "failed-api" || "$MIGRATION_OUTCOME" == "failed-verification" ]]; then
    if [[ -n "$id" ]]; then
      FAILED_DATASET_IDS="$(jq -nc --argjson ids "$FAILED_DATASET_IDS" --arg id "$id" '$ids + [$id] | unique')"
    fi
  fi

  print_progress
  if [[ "$index" -lt "$target_count" ]]; then
    [[ "$VERBOSE" == "true" ]] && echo "    waiting ${WAIT_SECONDS}s..."
    sleep "$WAIT_SECONDS"
  fi
done < <(jq -c '.[]' <<<"$targets")

migration_finished_epoch="$(date +%s)"
migration_duration_seconds=$((migration_finished_epoch - migration_started_epoch))

echo
echo "============================================================"
echo "Run summary"
echo "============================================================"
echo "Processed:              $processed / $target_count"
if [[ "$MODE" == "execute" ]]; then
  echo "Total datasets migrated: $migrated_count"
  echo "Total migration time:    $(format_duration "$migration_duration_seconds")"
  echo "Already migrated start: $initial_migrated"
else
  echo "Dry-run OK:             $dry_run_ok_count"
fi
echo "Skipped:                $skipped_count"
echo "Failed:                 $failed_count"
echo "Log written to:         $LOG_FILE"

echo
echo "Final migration status counts in Sigma:"
final_inventory_failed=0
if final_datasets="$(fetch_all_datasets)"; then
  jq -r 'group_by(.migrationStatus)[] | "  \(.[0].migrationStatus // "unknown"): \(length)"' <<<"$final_datasets"
  final_migrated="$(jq '[.[] | select(.migrationStatus == "migrated")] | length' <<<"$final_datasets")"
  echo
  echo "Final total migrated in Sigma: $final_migrated"
  [[ "$MODE" == "execute" ]] && echo "Newly migrated by this run:    $migrated_count"
else
  echo "  ERROR: final inventory could not be fetched"
  final_inventory_failed=1
fi

manage_error_workbook_badges

log_json "$(jq -nc \
  --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --argjson processed "$processed" \
  --argjson migrated "$migrated_count" \
  --argjson dry_run_ok "$dry_run_ok_count" \
  --argjson skipped "$skipped_count" \
  --argjson failed "$failed_count" \
  --arg owner_filter "$OWNER_FILTER" \
  --arg report_filter "$REPORT_FILTER" \
  --arg report_id "$RESOLVED_REPORT_ID" \
  --argjson duration_seconds "$migration_duration_seconds" \
  --argjson final_inventory_failed "$final_inventory_failed" \
  --argjson badges_updated "$BADGE_UPDATED_COUNT" \
  --argjson badges_skipped "$BADGE_SKIPPED_COUNT" \
  --argjson badge_failures "$BADGE_FAILED_COUNT" \
  '{timestamp:$timestamp,event:"run-summary",processed:$processed,migrated:$migrated,dryRunOk:$dry_run_ok,skipped:$skipped,failed:$failed,ownerFilter:$owner_filter,reportFilter:$report_filter,reportId:$report_id,durationSeconds:$duration_seconds,finalInventoryFailed:($final_inventory_failed == 1),badgesUpdated:$badges_updated,badgesSkipped:$badges_skipped,badgeFailures:$badge_failures}')"

[[ "$failed_count" -eq 0 && "$final_inventory_failed" -eq 0 && "$BADGE_FAILED_COUNT" -eq 0 ]]
