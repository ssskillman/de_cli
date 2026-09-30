#!/usr/bin/env bash
# de fivetran-transformations — pick a Fivetran transformation and run it.
#
# Lists dbt Core and Quickstart jobs from the Fivetran API (names match the
# Transformations UI), then POSTs /v1/transformations/{id}/run.
#
#   fivetran_transformations.sh                         # fzf, Enter runs
#   fivetran_transformations.sh list
#   fivetran_transformations.sh run NAME_OR_ID [--yes] [--full-refresh] [--force]
#
# Requires FIVETRAN_BASIC_AUTH (base64 of API key:secret), curl, and python3.
# fzf is used when present; otherwise a numbered prompt.
set -euo pipefail

BASE_URL="https://api.fivetran.com/v1"
YES="false"
FULL_REFRESH="false"
FORCE="false"

usage() {
  cat <<'EOF'
Usage:
  fivetran_transformations.sh
      Interactive fzf. Enter runs the highlighted transformation.

  fivetran_transformations.sh list
      Print name, type, status, and destination.

  fivetran_transformations.sh run NAME_OR_ID [--yes] [--full-refresh] [--force]

Paused jobs are skipped unless --force. A normal run sends full_refresh=false.
--full-refresh sends full_refresh=true. --yes skips the confirm prompt.

Requires FIVETRAN_BASIC_AUTH.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_auth() {
  [[ -n "${FIVETRAN_BASIC_AUTH:-}" ]] || die "FIVETRAN_BASIC_AUTH is unset"
  command -v python3 >/dev/null 2>&1 || die "python3 not on PATH"
  command -v curl >/dev/null 2>&1 || die "curl not on PATH"
}

# id<TAB>display rows. Column widths match the fzf header below.
# Optional human table when LIST_HUMAN=1 (no id column).
list_rows() {
  FIVETRAN_BASIC_AUTH="$FIVETRAN_BASIC_AUTH" \
  LIST_HUMAN="${LIST_HUMAN:-0}" \
  python3 - <<'PY'
import json, os, sys, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

auth = os.environ["FIVETRAN_BASIC_AUTH"]
human = os.environ.get("LIST_HUMAN") == "1"
NAME_W, TYPE_W, STATUS_W = 36, 12, 10

def api(path):
    req = urllib.request.Request(
        "https://api.fivetran.com/v1" + path,
        headers={
            "Authorization": "Basic " + auth,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"Fivetran {path} failed: {exc.code} {body}") from exc

def pages(path):
    items, cursor = [], None
    while True:
        q = path + ("&" if "?" in path else "?") + "limit=100"
        if cursor:
            q += "&cursor=" + urllib.parse.quote(cursor)
        data = api(q).get("data") or {}
        items.extend(data.get("items") or [])
        cursor = data.get("next_cursor")
        if not cursor:
            return items

def service_title(service):
    return " ".join(part.capitalize() for part in (service or "").replace("-", "_").split("_") if part)

def status_label(paused, status):
    if paused:
        return "Paused"
    labels = {
        "SUCCEEDED": "Succeeded",
        "RUNNING": "Running",
        "FAILED": "Failed",
        "BLOCKED": "Blocked",
        "PENDING": "Pending",
        "CANCELED": "Canceled",
        "CANCELLED": "Canceled",
        "SCHEDULING": "Scheduling",
    }
    return labels.get(status or "", status or "")

def type_label(kind):
    return {"DBT_CORE": "dbt Core", "QUICKSTART": "Quickstart"}.get(kind or "", kind or "")

groups = {g["id"]: g.get("name") or g["id"] for g in pages("/groups")}
projects = {
    p["id"]: p.get("group_id")
    for p in pages("/transformation-projects")
}
items = pages("/transformations")

def detail(item):
    return api("/transformations/" + urllib.parse.quote(item["id"]))["data"]

with ThreadPoolExecutor(max_workers=8) as pool:
    details = list(pool.map(detail, items))

conn_ids = []
for data in details:
    cfg = data.get("transformation_config") or {}
    if data.get("type") == "QUICKSTART":
        ids = cfg.get("connection_ids") or []
        if ids:
            conn_ids.append(ids[0])

def connection(cid):
    data = api("/connections/" + urllib.parse.quote(cid)).get("data") or {}
    return cid, data

conns = {}
if conn_ids:
    with ThreadPoolExecutor(max_workers=8) as pool:
        for cid, data in pool.map(connection, sorted(set(conn_ids))):
            conns[cid] = data

rows = []
for data in details:
    cfg = data.get("transformation_config") or {}
    kind = data.get("type") or ""
    if kind == "QUICKSTART":
        cid = (cfg.get("connection_ids") or [None])[0]
        conn = conns.get(cid) or {}
        service = conn.get("service") or cfg.get("package_name") or data["id"]
        schema = conn.get("schema") or ""
        name = service_title(service)
        if schema:
            name = f"{name}/{schema}"
        group_id = cfg.get("group_id")
    else:
        name = cfg.get("name") or data["id"]
        group_id = projects.get(cfg.get("project_id") or "")
    dest = groups.get(group_id or "", "")
    rows.append((
        data["id"],
        name,
        type_label(kind),
        status_label(bool(data.get("paused")), data.get("status")),
        dest,
        bool(data.get("paused")),
    ))

rows.sort(key=lambda row: row[1].casefold())
for tid, name, kind, status, dest, _paused in rows:
    display = f"{name[:NAME_W]:<{NAME_W}}  {kind:<{TYPE_W}}  {status:<{STATUS_W}}  {dest}"
    if human:
        print(display.rstrip())
    else:
        print(f"{tid}\t{display.rstrip()}\t{status}")
PY
}

pick_key() {
  local header="$1" prompt="$2"
  local input sel
  input=$(cat)
  [[ -n "$input" ]] || die "no transformations returned"

  if command -v fzf >/dev/null 2>&1 && [[ -e /dev/tty ]]; then
    if ! sel=$(printf '%s\n' "$input" | fzf \
      --height=70% \
      --reverse \
      --border=rounded \
      --cycle \
      --delimiter=$'\t' \
      --with-nth=2 \
      --prompt="$prompt" \
      --header="$header" \
      --pointer='▶'); then
      return 1
    fi
    printf '%s' "$sel"
    return 0
  fi

  echo >&2
  echo "$header" >&2
  local -a lines=()
  local line i=1
  while IFS= read -r line; do
    lines+=("$line")
    echo "  $i. ${line#*$'\t'}" >&2
    i=$((i + 1))
  done <<<"$input"
  local raw
  read -r -p "Choose number (0 cancels): " raw </dev/tty || return 1
  [[ "$raw" =~ ^[0-9]+$ ]] || return 1
  [[ "$raw" -ge 1 && "$raw" -le ${#lines[@]} ]] || return 1
  printf '%s' "${lines[$((raw - 1))]}"
}

# Resolve NAME_OR_ID against the live list. Prints id<TAB>name<TAB>status.
resolve() {
  local query="$1"
  list_rows | QUERY="$query" python3 -c '
import os, sys
query = os.environ["QUERY"].casefold()
hits = []
for line in sys.stdin:
    line = line.rstrip("\n")
    tid, display, status = line.split("\t", 2)
    name = display[:36].rstrip()
    if tid == os.environ["QUERY"] or name.casefold() == query:
        hits.append((tid, name, status))
if not hits:
    sys.exit(1)
if len(hits) > 1:
    sys.exit(2)
print("\t".join(hits[0]))
' || {
    local code=$?
    if [[ "$code" -eq 2 ]]; then
      die "more than one transformation matches '$query'"
    fi
    die "no transformation named '$query'"
  }
}

run_id() {
  local id="$1" name="$2" status="$3"
  if [[ "$status" == "Paused" && "$FORCE" != "true" ]]; then
    echo "ERROR: $name is paused. Unpause it in Fivetran, or pass --force." >&2
    return 1
  fi
  if [[ "$YES" != "true" ]]; then
    local answer
    read -r -p "Run ${name} (${id})? [y/N] " answer </dev/tty || return 1
    [[ "$answer" == "y" || "$answer" == "Y" ]] || return 1
  fi
  local body tmp code
  body=$(printf '{"full_refresh": %s}' "$FULL_REFRESH")
  tmp=$(mktemp)
  code=$(curl -sS -o "$tmp" -w '%{http_code}' \
    -X POST \
    -H "Accept: application/json" \
    -H "Content-Type: application/json" \
    -H "Authorization: Basic ${FIVETRAN_BASIC_AUTH}" \
    --data "$body" \
    "${BASE_URL}/transformations/${id}/run") || {
      rm -f "$tmp"
      echo "ERROR: request failed" >&2
      return 1
    }
  if [[ "$code" != 2* ]]; then
    echo "Fivetran rejected the run (HTTP ${code}):" >&2
    python3 -c 'import json,sys; raw=sys.stdin.read();
try:
    d=json.loads(raw); print(d.get("message") or d.get("code") or raw)
except Exception:
    print(raw)' <"$tmp" >&2
    rm -f "$tmp"
    return 1
  fi
  python3 -c 'import json,sys
d=json.load(sys.stdin).get("data") or {}
print("Triggered", sys.argv[1], "(" + sys.argv[2] + ")")
print("status:", d.get("status") or "accepted")
' "$name" "$id" <"$tmp"
  rm -f "$tmp"
}

interactive() {
  local cols sel id name status
  cols=$(printf '%-36s  %-12s  %-10s  %s' "Name" "Type" "Status" "Destination")
  while true; do
    echo "Loading Fivetran transformations..." >&2
    local rows
    rows=$(list_rows) || return 1
    if ! sel=$(printf '%s\n' "$rows" | pick_key "$cols  ·  Esc back" "run > "); then
      return 0
    fi
    id="${sel%%$'\t'*}"
    status="${sel##*$'\t'}"
    local display
    display="${sel#*$'\t'}"
    display="${display%$'\t'*}"
    name=$(python3 -c 'import sys; print(sys.argv[1][:36].rstrip())' "$display")
    if ! run_id "$id" "$name" "$status"; then
      echo "Not run." >&2
    fi
    echo >&2
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help|help) usage; exit 0 ;;
    --yes|-y) YES="true"; shift ;;
    --full-refresh) FULL_REFRESH="true"; shift ;;
    --force) FORCE="true"; shift ;;
    list|run) break ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

require_auth

cmd="${1:-}"
if [[ -n "$cmd" ]]; then
  shift
fi

case "$cmd" in
  "")
    interactive
    ;;
  list)
    [[ $# -eq 0 ]] || die "list takes no arguments"
    echo "Loading Fivetran transformations..." >&2
    printf '%-36s  %-12s  %-10s  %s\n' "Name" "Type" "Status" "Destination"
    LIST_HUMAN=1 list_rows
    ;;
  run)
    [[ $# -ge 1 ]] || die "run needs a transformation name or id"
    query="$1"
    shift
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --yes|-y) YES="true" ;;
        --full-refresh) FULL_REFRESH="true" ;;
        --force) FORCE="true" ;;
        *) die "unknown argument: $1" ;;
      esac
      shift
    done
    echo "Loading Fivetran transformations..." >&2
    resolved=$(resolve "$query")
    id="${resolved%%$'\t'*}"
    rest="${resolved#*$'\t'}"
    name="${rest%%$'\t'*}"
    status="${rest#*$'\t'}"
    run_id "$id" "$name" "$status"
    ;;
  *)
    die "unknown command: $cmd"
    ;;
esac
