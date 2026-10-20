#!/usr/bin/env python3
"""Confirm who can access each Iterable | Data & Analytics folder, then grant it."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sigma_ensure_workspace import (
    TREE,
    WORKSPACE_NAME,
    child_folders,
    find_workspaces,
    folder_id,
    workspace_parent_id,
)
from sigma_move_workbooks import (
    DEFAULT_ENV,
    DEFAULT_TOKEN_HELPER,
    SigmaApi,
    eprint,
    load_env,
    member_display,
    mint_token,
)
from sigma_swap_data_model_connection import fzf_command, fzf_pick

# Sigma's org-wide grantee. GET /v2/teams/{id} returns "Cannot get All Members team".
ALL_MEMBERS_TEAM_ID = "20140501-5fca-4000-8000-000000000001"
ALL_MEMBERS_LABEL = "All members of the organization"

# API values are view, explore, organize, edit. Labels match the Share dialog.
PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("explore", "Can explore"),
    ("view", "Can view"),
    ("organize", "Can contribute"),
    ("edit", "Can manage"),
)
PERMISSION_RANK = {name: index for index, (name, _label) in enumerate(
    (("view", ""), ("explore", ""), ("organize", ""), ("edit", ""))
)}
PERMISSION_LABEL = dict(PERMISSIONS)


class ShareError(RuntimeError):
    """A share cannot be applied safely."""


class Node:
    def __init__(self, kind: str, path: str, workspace_id: str, inode_id: str) -> None:
        self.kind = kind
        self.path = path
        self.workspace_id = workspace_id
        self.inode_id = inode_id


class ShareChoice:
    def __init__(self, grantees: list[tuple[str, str]], permission: str) -> None:
        self.grantees = grantees
        self.permission = permission


def permission_label(permission: str) -> str:
    return PERMISSION_LABEL.get(permission, permission)


def _named(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    needle = name.casefold()
    return [row for row in rows if str(row.get("name") or "").casefold() == needle]


def iter_nodes(api: SigmaApi) -> list[Node]:
    matches = find_workspaces(api, WORKSPACE_NAME)
    if len(matches) != 1:
        raise ShareError(
            f"ERROR: expected 1 workspace named {WORKSPACE_NAME!r}, found {len(matches)}"
        )
    workspace = matches[0]
    workspace_id = str(workspace.get("workspaceId") or "")
    root_id = workspace_parent_id(workspace)
    if not workspace_id:
        raise ShareError("ERROR: workspace has no workspaceId")
    nodes = [Node("workspace", WORKSPACE_NAME, workspace_id, root_id)]

    def walk(parent_id: str, prefix: str, tree: tuple[tuple[str, tuple], ...]) -> None:
        for name, children in tree:
            found = _named(child_folders(api, parent_id), name)
            if len(found) != 1:
                raise ShareError(
                    f"ERROR: expected 1 folder named {name!r} under {prefix}, found {len(found)}"
                )
            inode_id = folder_id(found[0])
            path = f"{prefix} / {name}"
            nodes.append(Node("folder", path, workspace_id, inode_id))
            walk(inode_id, path, children)

    walk(root_id, WORKSPACE_NAME, TREE)
    return nodes


def list_direct_grants(api: SigmaApi, inode_id: str) -> list[dict[str, Any]]:
    return api.paginate(
        "/v2/grants",
        {"inodeId": inode_id, "directGrantsOnly": "true", "limit": 200},
    )


def grantee_of(grant: dict[str, Any]) -> tuple[str, str] | None:
    team_id = grant.get("teamId")
    if team_id:
        return ("team", str(team_id))
    member_id = grant.get("memberId")
    if member_id:
        return ("member", str(member_id))
    return None


def grantee_label(
    kind: str,
    grantee_id: str,
    teams_by_id: dict[str, str],
    members_by_id: dict[str, str],
) -> str:
    if kind == "team" and grantee_id == ALL_MEMBERS_TEAM_ID:
        return ALL_MEMBERS_LABEL
    if kind == "team":
        return f"Team: {teams_by_id.get(grantee_id, grantee_id)}"
    return members_by_id.get(grantee_id, grantee_id)


def describe_grants(
    grants: list[dict[str, Any]],
    teams_by_id: dict[str, str],
    members_by_id: dict[str, str],
) -> str:
    if not grants:
        return "(no direct grants)"
    parts = []
    for grant in grants:
        who = grantee_of(grant)
        if who is None:
            continue
        parts.append(
            f"{grantee_label(who[0], who[1], teams_by_id, members_by_id)} — {permission_label(str(grant.get('permission') or ''))}"
        )
    return "; ".join(parts) if parts else "(no direct grants)"


def grant_action(grants: list[dict[str, Any]], kind: str, grantee_id: str, permission: str) -> str:
    """Return new, already, or raise. Never downgrade an existing grant."""
    field = "teamId" if kind == "team" else "memberId"
    ranks = [
        PERMISSION_RANK.get(str(grant.get("permission") or ""), -1)
        for grant in grants
        if str(grant.get(field) or "") == grantee_id
    ]
    if not ranks:
        return "new"
    if max(ranks) >= PERMISSION_RANK[permission]:
        return "already"
    return "raise"


def _grantee_body(kind: str, grantee_id: str) -> dict[str, str]:
    if kind == "team":
        return {"teamId": grantee_id}
    if kind == "member":
        return {"memberId": grantee_id}
    raise ShareError(f"ERROR: unknown grantee type {kind!r}")


def apply_grants(api: SigmaApi, node: Node, choice: ShareChoice) -> list[str]:
    """Grant access that is missing or lower. Leave equal or higher grants unchanged."""
    current = list_direct_grants(api, node.inode_id)
    fresh: list[tuple[str, str]] = []
    lines: list[str] = []
    for kind, grantee_id in choice.grantees:
        action = grant_action(current, kind, grantee_id, choice.permission)
        if action == "already":
            lines.append(f"already\t{node.path}\t{kind}:{grantee_id}\t{choice.permission}")
            continue
        fresh.append((kind, grantee_id))
    if not fresh:
        return lines
    if node.kind == "workspace":
        api.request(
            "POST",
            f"/v2/workspaces/{node.workspace_id}/grants",
            body={
                "grants": [
                    {"grantee": _grantee_body(kind, grantee_id), "permission": choice.permission}
                    for kind, grantee_id in fresh
                ]
            },
        )
    else:
        for kind, grantee_id in fresh:
            api.request(
                "POST",
                "/v2/grants",
                body={
                    "grantee": _grantee_body(kind, grantee_id),
                    "permission": choice.permission,
                    "inodeId": node.inode_id,
                },
            )
    verified = list_direct_grants(api, node.inode_id)
    for kind, grantee_id in fresh:
        if grant_action(verified, kind, grantee_id, choice.permission) == "new":
            raise ShareError(
                f"ERROR: grant for {kind}:{grantee_id} was not present on {node.path} after sharing"
            )
        lines.append(f"shared\t{node.path}\t{kind}:{grantee_id}\t{choice.permission}")
    return lines


def review(
    api: SigmaApi,
    nodes: list[Node],
    choose: Callable[[Node, list[dict[str, Any]]], ShareChoice | None],
    confirm: Callable[[Node, ShareChoice], str],
) -> list[str]:
    """choose returns None to skip. confirm returns apply, skip, or quit."""
    lines: list[str] = []
    for node in nodes:
        grants = list_direct_grants(api, node.inode_id)
        choice = choose(node, grants)
        if choice is None:
            lines.append(f"skipped\t{node.path}")
            continue
        decision = confirm(node, choice)
        if decision == "quit":
            lines.append(f"stopped\t{node.path}")
            return lines
        if decision != "apply":
            lines.append(f"skipped\t{node.path}")
            continue
        lines.extend(apply_grants(api, node, choice))
    return lines


def active_members(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("memberId") and not row.get("isArchived") and not row.get("isInactive")
    ]


def fzf_pick_multi(entries: list[tuple[str, str]], *, header: str, prompt: str) -> list[str]:
    if not entries:
        return []
    command = fzf_command(header=header, prompt=prompt)
    command.extend(["--multi", "--marker=◉ "])
    proc = subprocess.run(
        command,
        input="\n".join(f"{key}\t{label}" for key, label in entries) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    return [line.split("\t", 1)[0].strip() for line in proc.stdout.strip().split("\n")]


def prompt_choice(
    node: Node,
    grants: list[dict[str, Any]],
    teams: list[dict[str, Any]],
    members: list[dict[str, Any]],
    teams_by_id: dict[str, str],
    members_by_id: dict[str, str],
) -> ShareChoice | None:
    current = describe_grants(grants, teams_by_id, members_by_id)
    scope = "This grant covers every folder inside it." if node.kind == "workspace" else (
        "A parent grant already covers this folder. A grant here only adds access."
    )
    header = f"{node.path}\nNow: {current}\n{scope}"
    entries = [
        ("all-members", ALL_MEMBERS_LABEL),
        ("people", "Pick people…"),
        ("skip", "Skip — leave unchanged"),
    ]
    for team in sorted(teams, key=lambda row: str(row.get("name") or "").casefold()):
        if team.get("isArchived") or not team.get("teamId"):
            continue
        entries.append((f"team:{team['teamId']}", f"Team: {team.get('name')}"))
    picked = fzf_pick(entries, header=header, prompt="share > ")
    if not picked or picked == "skip":
        return None
    if picked == "people":
        people = fzf_pick_multi(
            [
                (str(row["memberId"]), member_display(row))
                for row in sorted(active_members(members), key=lambda row: member_display(row).casefold())
            ],
            header=f"People for {node.path}\nType to filter. Tab marks. Enter accepts.",
            prompt="people > ",
        )
        if not people:
            return None
        grantees = [("member", member_id) for member_id in people]
    elif picked == "all-members":
        grantees = [("team", ALL_MEMBERS_TEAM_ID)]
    elif picked.startswith("team:"):
        grantees = [("team", picked.split(":", 1)[1])]
    else:
        return None
    permission = fzf_pick(
        [(name, label) for name, label in PERMISSIONS],
        header=f"Access for {node.path}\nCan explore is the Share dialog default.",
        prompt="access > ",
    )
    if permission not in PERMISSION_RANK:
        return None
    return ShareChoice(grantees, permission)


def prompt_confirm(node: Node, choice: ShareChoice, teams_by_id: dict[str, str], members_by_id: dict[str, str]) -> str:
    who = ", ".join(
        grantee_label(kind, grantee_id, teams_by_id, members_by_id)
        for kind, grantee_id in choice.grantees
    )
    eprint(f"\n{node.path}")
    eprint(f"  Share with: {who}")
    eprint(f"  Access: {permission_label(choice.permission)}")
    eprint("Enter to share, N to skip, Q to stop.")
    try:
        answer = input("> ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        return "quit"
    if answer in {"q", "quit"}:
        return "quit"
    if answer in {"n", "no"}:
        return "skip"
    return "apply"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Walk Iterable | Data & Analytics and confirm who to share each "
            "workspace and folder with. The default grantee is all members of "
            "the organization. Nothing is shared until you confirm that node."
        )
    )
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--token-helper", type=Path, default=DEFAULT_TOKEN_HELPER)
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        eprint("ERROR: run this in a terminal so you can confirm each folder.")
        return 1

    load_env(args.env)
    base_url = os.environ.get("SIGMA_BASE_URL", "").rstrip("/")
    if not base_url:
        eprint("ERROR: SIGMA_BASE_URL is empty")
        return 1
    token = os.environ.get("SIGMA_API_TOKEN") or mint_token(args.token_helper)
    api = SigmaApi(base_url, token)
    teams = api.paginate("/v2/teams", {"limit": 200})
    members = api.paginate(
        "/v2/members",
        {"limit": 1000, "includeArchived": "true", "includeInactive": "true"},
    )
    teams_by_id = {str(row.get("teamId")): str(row.get("name") or "") for row in teams if row.get("teamId")}
    members_by_id = {
        str(row.get("memberId")): member_display(row) for row in members if row.get("memberId")
    }
    try:
        nodes = iter_nodes(api)
    except ShareError as exc:
        eprint(exc)
        return 1
    eprint(
        "A grant on a workspace or folder covers everything inside it. "
        "Sharing a folder does not hide it from someone who already has the parent."
    )
    eprint(f"{len(nodes)} nodes. Default grantee: {ALL_MEMBERS_LABEL}.")

    def choose(node: Node, grants: list[dict[str, Any]]) -> ShareChoice | None:
        return prompt_choice(node, grants, teams, members, teams_by_id, members_by_id)

    def confirm(node: Node, choice: ShareChoice) -> str:
        return prompt_confirm(node, choice, teams_by_id, members_by_id)

    try:
        lines = review(api, nodes, choose, confirm)
    except ShareError as exc:
        eprint(exc)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
