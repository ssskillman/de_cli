#!/usr/bin/env bash
# de astro-users — provision / deprovision Astronomer organization, workspace, or deployment users.
#
# Organization remove is UI-only: the Astro CLI and public Cloud API expose no
# call for it, so that action opens Astronomer instead.
# Workspace and deployment removes only drop a direct role.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  astro_users.sh
      Interactive: organization, workspace, or deployment, then list / add / update / remove.

  astro_users.sh organization list
  astro_users.sh organization add EMAIL [--role ROLE]
  astro_users.sh organization update EMAIL --role ROLE
  astro_users.sh organization remove EMAIL

  astro_users.sh workspace list
  astro_users.sh workspace add EMAIL [--role ROLE]
  astro_users.sh workspace update EMAIL --role ROLE
  astro_users.sh workspace remove EMAIL

  astro_users.sh deployment list --deployment-id ID
  astro_users.sh deployment add EMAIL --deployment-id ID [--role ROLE]
  astro_users.sh deployment update EMAIL --deployment-id ID --role ROLE
  astro_users.sh deployment remove EMAIL --deployment-id ID

Organization roles: ORGANIZATION_MEMBER (default), ORGANIZATION_BILLING_ADMIN,
ORGANIZATION_OWNER. Organization remove is not available from the CLI or API;
it opens Astronomer so you can use Organization → Users → Remove member.

Workspace roles: WORKSPACE_MEMBER (default), WORKSPACE_AUTHOR,
WORKSPACE_OPERATOR, WORKSPACE_OWNER.

Deployment roles: DEPLOYMENT_ADMIN (default) or a custom role name.

Workspace remove drops the user from every deployment in the current workspace
unless they still have team access. It does not remove them from the Organization.

Uses the current Astro workspace (`astro workspace list` / `astro context`).
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

require_astro() {
  command -v astro >/dev/null 2>&1 || die "astro CLI not on PATH (astro login)"
}

show_workspace() {
  echo "Current Astro workspace:" >&2
  astro workspace list 2>/dev/null | sed $'s/\x1b\\[[0-9;]*m//g' >&2 || true
}

# Printed by pick_key on Esc / cancel. Callers walk up one menu, they do not exit.
BACK_KEY="__back__"

# key<TAB>display rows. Esc / 0 → BACK_KEY (does not exit).
# Optional third arg is the fzf height (default 40%).
pick_key() {
  local header="$1" prompt="$2" height="${3:-40%}"
  local input sel
  input=$(cat)
  [[ -n "$input" ]] || die "nothing to pick ($header)"

  if command -v fzf >/dev/null 2>&1 && [[ -e /dev/tty ]]; then
    # fzf reads the list on stdin and the keyboard from /dev/tty itself.
    if ! sel=$(printf '%s\n' "$input" | fzf \
      --height="$height" \
      --reverse \
      --border=rounded \
      --cycle \
      --bind='up:transform:[ "$FZF_POS" -eq 1 ] && echo last || echo up' \
      --bind='down:transform:[ "$FZF_POS" -ge "$FZF_MATCH_COUNT" ] && echo first || echo down' \
      --delimiter=$'\t' \
      --with-nth=2 \
      --prompt="$prompt" \
      --header="$header  ·  Esc back" \
      --pointer='▶'); then
      printf '%s' "$BACK_KEY"
      return 0
    fi
    printf '%s' "${sel%%$'\t'*}"
    return 0
  fi

  echo >&2
  echo "$header" >&2
  local -a keys=()
  local line key label i=1
  while IFS= read -r line; do
    key="${line%%$'\t'*}"
    label="${line#*$'\t'}"
    keys+=("$key")
    echo "  $i. $label" >&2
    i=$((i + 1))
  done <<<"$input"
  echo "  0. Back" >&2
  local raw
  read -r -p "Choose number: " raw </dev/tty || { printf '%s' "$BACK_KEY"; return 0; }
  [[ "$raw" =~ ^[0-9]+$ ]] || { printf '%s' "$BACK_KEY"; return 0; }
  [[ "$raw" -ge 1 && "$raw" -le ${#keys[@]} ]] || { printf '%s' "$BACK_KEY"; return 0; }
  printf '%s' "${keys[$((raw - 1))]}"
}

prompt_line() {
  local label="$1" value
  read -r -p "$label" value </dev/tty || return 1
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  [[ -n "$value" ]] || return 1
  printf '%s' "$value"
}

deployment_rows() {
  astro deployment list 2>/dev/null | awk '
    {
      id = ""
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^c[a-z0-9]{16,}$/) id = $i
      }
      if (id == "" || $1 == "NAME") next
      printf "%s\t%s  %s\n", id, $1, id
    }
  '
}

# email<TAB>name role — from the current workspace or deployment.
user_rows() {
  local scope="$1" dep_id="${2:-}"
  local -a cmd=(astro "$scope" user list --json --verbosity error)
  if [[ -n "$dep_id" ]]; then
    cmd+=(--deployment-id "$dep_id")
  fi
  "${cmd[@]}" 2>/dev/null | python3 -c '
import json, sys
raw = sys.stdin.read()
i = 0
while i < len(raw) and raw[i] not in "{[":
    i += 1
if i >= len(raw):
    sys.exit(0)
data, _ = json.JSONDecoder().raw_decode(raw, i)
users = data.get("users") if isinstance(data, dict) else data
rows = []
for user in users or []:
    email = (user.get("email") or "").strip()
    if not email:
        continue
    name = (user.get("fullName") or "").strip()
    role = (
        user.get("orgRole")
        or user.get("deploymentRole")
        or user.get("workspaceRole")
        or user.get("role")
        or ""
    ).strip()
    rows.append((email, name, role))
if not rows:
    sys.exit(0)
name_w = min(36, max((len(n) for _, n, _ in rows if n), default=0))
name_w = max(name_w, 12)
email_w = min(42, max(len(e) for e, _, _ in rows))
for email, name, role in rows:
    display = f"{name[:name_w]:<{name_w}}  {email:<{email_w}}  {role}".rstrip()
    print(email + "\t" + display)
'
}

pick_user() {
  local scope="$1" dep_id="${2:-}" header="$3"
  local rows
  rows=$(user_rows "$scope" "$dep_id")
  if [[ -z "$rows" ]]; then
    echo "No users to pick." >&2
    printf '%s' "$BACK_KEY"
    return 0
  fi
  printf '%s\n' "$rows" | pick_key "$header" "user > "
}

pick_role() {
  local scope="$1"
  if [[ "$scope" == "organization" ]]; then
    pick_key "Organization role" "role > " <<'EOF'
ORGANIZATION_MEMBER	ORGANIZATION_MEMBER (default)
ORGANIZATION_BILLING_ADMIN	ORGANIZATION_BILLING_ADMIN
ORGANIZATION_OWNER	ORGANIZATION_OWNER
EOF
    return
  fi
  if [[ "$scope" == "workspace" ]]; then
    pick_key "Workspace role" "role > " <<'EOF'
WORKSPACE_MEMBER	WORKSPACE_MEMBER (default)
WORKSPACE_AUTHOR	WORKSPACE_AUTHOR
WORKSPACE_OPERATOR	WORKSPACE_OPERATOR
WORKSPACE_OWNER	WORKSPACE_OWNER
EOF
    return
  fi
  local choice
  choice=$(pick_key "Deployment role" "role > " <<'EOF'
DEPLOYMENT_ADMIN	DEPLOYMENT_ADMIN (default)
custom	Type a custom role name
EOF
)
  if [[ "$choice" == "$BACK_KEY" ]]; then
    printf '%s' "$BACK_KEY"
    return 0
  fi
  if [[ "$choice" == "custom" ]]; then
    local name
    if ! name=$(prompt_line "Custom role name: "); then
      printf '%s' "$BACK_KEY"
      return 0
    fi
    printf '%s' "$name"
    return 0
  fi
  printf '%s' "$choice"
}

# Neither the Astro CLI nor the public Cloud API can remove an Organization
# member (`astro api cloud ls` has no DELETE on users). Only the UI can.
remove_org_user() {
  local email="$1"
  echo >&2
  echo "Organization remove is UI-only. The Astro CLI and public API have no call for it." >&2
  echo "Remove ${email} in Astronomer: Organization → Users → ⋯ → Remove member." >&2
  if command -v open >/dev/null 2>&1; then
    open "https://cloud.astronomer.io" >/dev/null 2>&1 || true
  fi
  return 1
}

run_astro() {
  local scope="$1" action="$2" email="${3:-}" role="${4:-}" dep_id="${5:-}"
  if [[ "$scope" == "organization" && "$action" == "remove" ]]; then
    remove_org_user "$email"
    return
  fi
  local astro_action="$action"
  if [[ "$scope" == "organization" && "$action" == "add" ]]; then
    astro_action="invite"
  fi
  local -a cmd=(astro "$scope" user "$astro_action")
  if [[ -n "$email" ]]; then
    cmd+=("$email")
  fi
  if [[ -n "$role" ]]; then
    cmd+=(--role "$role")
  fi
  if [[ -n "$dep_id" ]]; then
    cmd+=(--deployment-id "$dep_id")
  fi
  printf '→' >&2
  printf ' %q' "${cmd[@]}" >&2
  printf '\n' >&2
  "${cmd[@]}"
}

interactive() {
  require_astro
  show_workspace
    local scope action email role dep_id rows choice
  while true; do
    scope=$(pick_key "Astronomer access" "scope > " <<'EOF'
organization	Organization members (list, invite, change role; remove opens the UI)
workspace	Workspace users (all deployments in this workspace)
deployment	Deployment users (one deployment)
EOF
)
    [[ "$scope" == "$BACK_KEY" ]] && return 0

    dep_id=""
    if [[ "$scope" == "deployment" ]]; then
      rows=$(deployment_rows)
      [[ -n "$rows" ]] || die "no deployments from \`astro deployment list\`"
      dep_id=$(printf '%s\n' "$rows" | pick_key "Deployment" "deployment > ")
      [[ "$dep_id" == "$BACK_KEY" ]] && continue
    fi

    # The picker is the user list. ↑/↓ moves through people. Enter then
    # chooses remove or update for that person. Add is a row at the top.
    while true; do
      rows=$(user_rows "$scope" "$dep_id")
      choice=$(
        {
          printf '%s\t%s\n' "__add__" "Add user"
          if [[ -n "$rows" ]]; then
            printf '%s\n' "$rows"
          fi
          printf '%s\t%s\n' "$BACK_KEY" "Back"
        } | pick_key "↑/↓ moves through users · Enter selects · Esc back" "user > " "70%"
      )
      [[ "$choice" == "$BACK_KEY" ]] && break

      email=""
      role=""
      action=""
      if [[ "$choice" == "__add__" ]]; then
        email=$(prompt_line "Email: ") || continue
        role=$(pick_role "$scope")
        [[ "$role" == "$BACK_KEY" ]] && continue
        action="add"
      else
        email="$choice"
        action=$(pick_key "$email" "action > " <<EOF
remove	Remove ${email}
update	Update role
${BACK_KEY}	Back
EOF
)
        [[ "$action" == "$BACK_KEY" ]] && continue
        if [[ "$action" == "update" ]]; then
          role=$(pick_role "$scope")
          [[ "$role" == "$BACK_KEY" ]] && continue
        fi
        if [[ "$scope" == "organization" && "$action" == "remove" ]]; then
          echo >&2
          echo "Organization remove can't be done from here; opening Astronomer." >&2
        fi
        if [[ "$scope" == "workspace" && "$action" == "remove" ]]; then
          echo >&2
          echo "This removes ${email} from every deployment in the current workspace unless they still have team access. It does not remove them from the Organization." >&2
        fi
        if [[ "$scope" == "deployment" && "$action" == "remove" ]]; then
          echo >&2
          echo "This removes ${email}'s direct role on the deployment. They may still have access through a team." >&2
        fi
      fi
      if ! run_astro "$scope" "$action" "$email" "$role" "$dep_id"; then
        if [[ "$action" == "remove" && "$scope" != "organization" ]]; then
          echo "That email has no direct ${scope} role. Organization members are removed from the Organization list, not from a deployment." >&2
        fi
      fi
    done
  done
}

# --- non-interactive ---
parse_and_run() {
  local scope="$1" action="$2"
  shift 2
  [[ "$scope" == "organization" || "$scope" == "workspace" || "$scope" == "deployment" ]] \
    || die "scope must be organization, workspace, or deployment (got $scope)"
  [[ "$action" == "list" || "$action" == "add" || "$action" == "update" || "$action" == "remove" ]] \
    || die "action must be list, add, update, or remove (got $action)"

  local email="" role="" dep_id=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --role)
        [[ $# -ge 2 ]] || die "--role needs a value"
        role="$2"
        shift 2
        ;;
      --role=*)
        role="${1#--role=}"
        shift
        ;;
      --deployment-id)
        [[ $# -ge 2 ]] || die "--deployment-id needs a value"
        dep_id="$2"
        shift 2
        ;;
      --deployment-id=*)
        dep_id="${1#--deployment-id=}"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      --*)
        die "unknown flag: $1"
        ;;
      *)
        if [[ -n "$email" ]]; then
          die "unexpected argument: $1"
        fi
        email="$1"
        shift
        ;;
    esac
  done

  if [[ "$scope" == "deployment" && -z "$dep_id" ]]; then
    die "deployment $action requires --deployment-id"
  fi
  if [[ "$action" == "list" ]]; then
    [[ -z "$email" ]] || die "list does not take an email"
    [[ -z "$role" ]] || die "list does not take --role"
  else
    [[ -n "$email" ]] || die "$action requires an email"
  fi
  if [[ "$action" == "update" && -z "$role" ]]; then
    die "update requires --role"
  fi
  if [[ "$action" == "remove" && -n "$role" ]]; then
    die "remove does not take --role"
  fi

  require_astro
  show_workspace
  if [[ "$scope" == "workspace" && "$action" == "remove" ]]; then
    echo "Workspace remove drops this user from every deployment in the current workspace unless they still have team access. It does not remove them from the Organization." >&2
  fi
  run_astro "$scope" "$action" "$email" "$role" "$dep_id"
}

if [[ $# -eq 0 ]]; then
  interactive
  exit 0
fi

case "$1" in
  -h|--help|help)
    usage
    exit 0
    ;;
esac

[[ $# -ge 2 ]] || die "need: workspace|deployment and list|add|update|remove (or no args for the menu)"
parse_and_run "$@"
