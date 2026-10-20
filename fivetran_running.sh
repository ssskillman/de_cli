#!/usr/bin/env bash
# de fivetran-running — transformations and connectors that are running now.
#
# Right arrow shows the platform log for the highlighted job.
# Enter prints that log, then returns to the list. Nothing is started.
#
#   fivetran_running.sh
#   fivetran_running.sh list
#   fivetran_running.sh log xf:ID
#   fivetran_running.sh log conn:ID
#
# Requires FIVETRAN_BASIC_AUTH. Connector and transformation logs are read from
# FIVETRAN_DB.FIVETRAN_LOG.LOG (the platform connector lags the live sync).
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  fivetran_running.sh
      fzf of running transformations and syncing connectors.
      Right arrow shows the log. Up/Down wrap.

  fivetran_running.sh list
  fivetran_running.sh log xf:TRANSFORMATION_ID
  fivetran_running.sh log conn:CONNECTION_ID

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
}

# key<TAB>display
# key is xf:<transformation id> or conn:<connection id>
list_rows() {
  FIVETRAN_BASIC_AUTH="$FIVETRAN_BASIC_AUTH" python3 - <<'PY'
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

auth = os.environ["FIVETRAN_BASIC_AUTH"]
KIND_W, NAME_W, STATUS_W = 16, 42, 22

def api(path, attempts=3):
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(
            "https://api.fivetran.com/v1" + path,
            headers={"Authorization": "Basic " + auth, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            raise SystemExit(f"Fivetran {path} failed: {exc.code} {body}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(0.4 * (attempt + 1))
    raise SystemExit(f"Fivetran {path} failed: {last}")

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
    return " ".join(
        part.capitalize()
        for part in (service or "").replace("-", "_").split("_")
        if part
    )

groups = {g["id"]: g.get("name") or g["id"] for g in pages("/groups")}
projects = {p["id"]: p.get("group_id") for p in pages("/transformation-projects")}
rows = []

running_status = {"RUNNING", "PENDING", "SCHEDULING"}
xf_items = [it for it in pages("/transformations") if (it.get("status") or "") in running_status]

def xf_name(item):
    data = api("/transformations/" + urllib.parse.quote(item["id"])).get("data") or {}
    cfg = data.get("transformation_config") or {}
    if data.get("type") == "QUICKSTART":
        return cfg.get("package_name") or item["id"], cfg.get("group_id")
    return cfg.get("name") or item["id"], projects.get(cfg.get("project_id") or "")

if xf_items:
    with ThreadPoolExecutor(max_workers=8) as pool:
        named = list(pool.map(xf_name, xf_items))
    for item, (name, group_id) in zip(xf_items, named):
        dest = groups.get(group_id or "", "")
        status = {"RUNNING": "Running", "PENDING": "Pending", "SCHEDULING": "Scheduling"}.get(
            item.get("status") or "", item.get("status") or ""
        )
        display = f"{'Transformation':<{KIND_W}}  {name[:NAME_W]:<{NAME_W}}  {status:<{STATUS_W}}  {dest}"
        rows.append((name.casefold(), f"xf:{item['id']}\t{display.rstrip()}"))

for conn in pages("/connections"):
    status = conn.get("status") or {}
    if status.get("sync_state") != "syncing":
        continue
    name = service_title(conn.get("service"))
    schema = conn.get("schema") or ""
    if schema:
        name = f"{name}/{schema}"
    label = "Syncing historical" if status.get("is_historical_sync") else "Syncing"
    dest = groups.get(conn.get("group_id") or "", "")
    display = f"{'Connector':<{KIND_W}}  {name[:NAME_W]:<{NAME_W}}  {label:<{STATUS_W}}  {dest}"
    rows.append((name.casefold(), f"conn:{conn['id']}\t{display.rstrip()}"))

rows.sort(key=lambda row: (0 if row[1].startswith("xf:") else 1, row[0]))
for _, line in rows:
    print(line)
PY
}

print_conn_log() {
  local id="$1"
  local cache="${HOME}/.cache/de/ft-logs/conn-${id}"
  if [[ -f "$cache" ]]; then
    local age=$(( $(date +%s) - $(stat -f %m "$cache") ))
    if [[ "$age" -lt 90 ]]; then
      cat "$cache"
      return 0
    fi
  fi
  local py="$HOME/github/udi-dbt/.venv/bin/python"
  if [[ ! -x "$py" ]]; then
    py=$(command -v python3)
  fi
  local tmp
  tmp=$(mktemp)
  if ! CONNECTION_ID="$id" FIVETRAN_BASIC_AUTH="$FIVETRAN_BASIC_AUTH" "$py" - >"$tmp" <<'PY'
import contextlib, io, json, os, sys, urllib.request
from pathlib import Path

cid = os.environ["CONNECTION_ID"]
auth = os.environ.get("FIVETRAN_BASIC_AUTH") or ""

def service_title(service):
    return " ".join(part.capitalize() for part in (service or "").replace("-", "_").split("_") if part)

name = cid
if auth:
    try:
        req = urllib.request.Request(
            "https://api.fivetran.com/v1/connections/" + cid,
            headers={"Authorization": "Basic " + auth, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp).get("data") or {}
        label = service_title(data.get("service"))
        schema = data.get("schema") or ""
        name = f"{label}/{schema}" if schema else (label or cid)
    except Exception:
        name = cid

try:
    import snowflake.connector
    import yaml
except ImportError:
    print("Snowflake client is not available (udi-dbt .venv).")
    raise SystemExit(1)

profile = yaml.safe_load((Path.home() / ".dbt" / "profiles.yml").read_text())
udi = profile["udi"]
out = udi["outputs"][udi.get("target") or "dev"]
buf = io.StringIO()
try:
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        conn = snowflake.connector.connect(
            account=out["account"],
            user=out["user"],
            role=out.get("role"),
            warehouse=out.get("warehouse"),
            database="FIVETRAN_DB",
            schema="FIVETRAN_LOG",
            authenticator=out.get("authenticator") or "externalbrowser",
            login_timeout=25,
            client_store_temporary_credential=True,
        )
except Exception as exc:
    print(f"Snowflake login failed: {exc}")
    raise SystemExit(1)

sql = """
WITH start_at AS (
  SELECT MAX(time_stamp) AS ts
  FROM FIVETRAN_DB.FIVETRAN_LOG.LOG
  WHERE connection_id = %s
    AND message_event = 'sync_start'
)
SELECT
  message_event,
  TO_VARCHAR(time_stamp, 'YYYY-MM-DD HH24:MI:SS') AS ts,
  message_data
FROM FIVETRAN_DB.FIVETRAN_LOG.LOG
WHERE connection_id = %s
  AND time_stamp >= (SELECT ts FROM start_at)
  AND message_event IN (
    'sync_start', 'sync_end', 'sync_stats', 'extract_summary', 'warning', 'info', 'error'
  )
ORDER BY time_stamp
"""
try:
    cur = conn.cursor()
    cur.execute(sql, (cid, cid))
    rows = cur.fetchall()
finally:
    conn.close()

def detail(event, raw):
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return raw or ""
    if event == "sync_start":
        return data.get("sync_type") or ""
    if event == "sync_end":
        bits = [data.get("status") or "", data.get("sync_type") or ""]
        return "  ".join(b for b in bits if b)
    if event == "warning":
        return data.get("message") or data.get("type") or ""
    if event == "info":
        return data.get("message") or data.get("type") or ""
    if event == "error":
        return data.get("message") or data.get("reason") or ""
    if event == "extract_summary":
        bits = [
            data.get("status") or "",
            f"{data.get('total_queries')} queries" if data.get("total_queries") is not None else "",
            f"{data.get('total_rows')} rows" if data.get("total_rows") is not None else "",
            data.get("rounded_total_size") or "",
        ]
        return "  ".join(str(b) for b in bits if b)
    if event == "sync_stats":
        bits = []
        if data.get("total_time_s") is not None:
            bits.append(f"total {data.get('total_time_s')}s")
        if data.get("extract_time_s") is not None:
            bits.append(f"extract {data.get('extract_time_s')}s")
        if data.get("load_time_s") is not None:
            bits.append(f"load {data.get('load_time_s')}s")
        return "  ".join(bits)
    return data.get("message") or ""

print(f"{name}  connector")
if not rows:
    print("No platform log for this connector yet.")
    print("Fivetran writes it to FIVETRAN_DB.FIVETRAN_LOG.LOG after the log connector syncs.")
    raise SystemExit(0)

print("Latest sync in the platform log (this lags the live run).")
print()
open_sync = True
for event, ts, raw in rows:
    if event == "sync_end":
        open_sync = False
    line = detail(event, raw)
    print(f"{ts}  {event:<16}  {line}".rstrip())
if open_sync:
    print()
    print("No sync_end yet. The sync may still be running, or the end event has not landed.")
PY
  then
    cat "$tmp" >&2
    rm -f "$tmp"
    return 1
  fi
  mkdir -p "${HOME}/.cache/de/ft-logs"
  cp "$tmp" "$cache"
  cat "$tmp"
  rm -f "$tmp"
}

show_log() {
  local key="$1"
  local kind="${key%%:*}"
  local id="${key#*:}"
  case "$kind" in
    xf)
      "$HOME/de/fivetran_transformations.sh" log "$id"
      ;;
    conn)
      print_conn_log "$id"
      ;;
    *)
      die "unknown log key: $key"
      ;;
  esac
}

pick_key() {
  local header="$1" prompt="$2"
  local input sel
  input=$(cat)
  [[ -n "$input" ]] || return 1

  if command -v fzf >/dev/null 2>&1 && [[ -e /dev/tty ]]; then
    # fzf expands $FZF_POS. The shell must not.
    # shellcheck disable=SC2016
    if ! sel=$(printf '%s\n' "$input" | fzf \
      --height=70% \
      --reverse \
      --border=rounded \
      --cycle \
      --delimiter=$'\t' \
      --with-nth=2 \
      --prompt="$prompt" \
      --header="$header" \
      --pointer='▶' \
      --preview-window='right:58%:wrap:hidden' \
      --preview "$0 log {1}" \
      --bind='right:toggle-preview' \
      --bind='up:transform:[ "$FZF_POS" -eq 1 ] && echo last || echo up' \
      --bind='down:transform:[ "$FZF_POS" -ge "$FZF_MATCH_COUNT" ] && echo first || echo down'); then
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

interactive() {
  local cols sel key
  cols=$(printf '%-16s  %-42s  %-22s  %s  ·  → log  ·  Esc back' "Kind" "Name" "Status" "Destination")
  while true; do
    echo "Loading running Fivetran jobs..." >&2
    local rows
    if ! rows=$(list_rows); then
      echo "Could not load running jobs." >&2
      local answer
      read -r -p "Retry? [Y/n] " answer </dev/tty || return 0
      [[ "$answer" == "n" || "$answer" == "N" ]] && return 0
      continue
    fi
    if [[ -z "$rows" ]]; then
      echo "Nothing is running. No connector is syncing, and no transformation is running." >&2
      local answer
      read -r -p "Refresh? [Y/n] " answer </dev/tty || return 0
      [[ "$answer" == "n" || "$answer" == "N" ]] && return 0
      continue
    fi
    if ! sel=$(printf '%s\n' "$rows" | pick_key "$cols" "running > "); then
      return 0
    fi
    key="${sel%%$'\t'*}"
    echo >&2
    show_log "$key" || true
    echo >&2
  done
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help|help) usage; exit 0 ;;
    list|log) break ;;
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
    rows=$(list_rows) || exit 1
    if [[ -z "$rows" ]]; then
      echo "Nothing is running."
      exit 0
    fi
    printf '%-16s  %-42s  %-22s  %s\n' "Kind" "Name" "Status" "Destination"
    printf '%s\n' "$rows" | cut -f2-
    ;;
  log)
    [[ $# -eq 1 ]] || die "log needs xf:ID or conn:ID"
    show_log "$1"
    ;;
  *)
    die "unknown command: $cmd"
    ;;
esac
