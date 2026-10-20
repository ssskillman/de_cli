#!/usr/bin/env python3
"""Transfer one Sigma workbook to a new owner."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sigma_move_workbooks import (
    DEFAULT_ENV,
    DEFAULT_TOKEN_HELPER,
    SigmaApi,
    fzf_pick,
    load_env,
    member_display,
    mint_token,
    parse_workbook_inode_ref,
    resolve_member,
)


def resolve_workbook_reference(api: Any, reference: str) -> dict[str, Any]:
    inode_id = parse_workbook_inode_ref(reference)
    if not inode_id:
        parsed = urllib.parse.urlsplit(reference.strip())
        parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
        try:
            workbook_index = parts.index("workbook")
            url_id = parts[workbook_index + 1]
        except (ValueError, IndexError):
            raise ValueError("provide one valid Sigma workbook URL or inode ID")

        matches: list[dict[str, Any]] = []
        pasted_url_id = url_id.casefold()
        for archived in (False, True):
            rows = api.paginate(
                "/v2/workbooks",
                {
                    "limit": 1000,
                    "skipPermissionCheck": "true",
                    "isArchived": str(archived).lower(),
                },
            )
            for row in rows:
                candidate = str(
                    row.get("workbookUrlId") or row.get("urlId") or ""
                )
                candidate_folded = candidate.casefold()
                if candidate_folded and (
                    pasted_url_id == candidate_folded
                    or pasted_url_id.endswith("-" + candidate_folded)
                ):
                    matches.append(row)
            if matches:
                break
        if len(matches) != 1:
            raise ValueError(
                f"workbook URL matched {len(matches)} workbooks (urlId={url_id})"
            )
        inode_id = str(matches[0].get("workbookId") or matches[0].get("id") or "")
        if not inode_id:
            raise ValueError(f"workbook has no inode ID (urlId={url_id})")

    workbook = api.request("GET", f"/v2/files/{inode_id}")
    if workbook.get("type") != "workbook":
        raise ValueError(
            f"{inode_id} is not a workbook (type={workbook.get('type')})"
        )
    workbook["id"] = inode_id
    return workbook


def reassign_workbook(
    api: Any, inode_id: str, new_owner_id: str
) -> dict[str, Any]:
    workbook = api.request("GET", f"/v2/files/{inode_id}")
    if workbook.get("type") != "workbook":
        raise ValueError(
            f"{inode_id} is not a workbook (type={workbook.get('type')})"
        )
    api.request(
        "PATCH",
        f"/v2/files/{inode_id}",
        body={"ownerId": new_owner_id},
    )
    verified = api.request("GET", f"/v2/files/{inode_id}")
    if str(verified.get("ownerId") or "") != new_owner_id:
        raise RuntimeError(
            f"ownership verification failed: ownerId={verified.get('ownerId')}"
        )
    return verified


def prompt_workbook() -> str:
    try:
        value = input("Paste one Sigma workbook URL or inode ID: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("\nCanceled.")
    if not value:
        raise SystemExit("Canceled.")
    return value


def pick_new_owner(members: list[dict[str, Any]]) -> dict[str, Any] | None:
    active = [
        member
        for member in members
        if not member.get("isArchived") and not member.get("isInactive")
    ]
    rows = [
        (str(member["memberId"]), member_display(member))
        for member in sorted(active, key=lambda row: str(row.get("email") or ""))
    ]
    member_id = fzf_pick(
        rows,
        header="Transfer this workbook to one owner",
        prompt="new owner > ",
        height="80%",
    )
    if not member_id:
        return None
    return next(
        member for member in active if str(member.get("memberId")) == member_id
    )


def member_label(member_id: str, members: list[dict[str, Any]]) -> str:
    for member in members:
        if str(member.get("memberId") or "") == member_id:
            return member_display(member)
    return member_id or "(unassigned)"


def confirm_transfer(workbook_name: str) -> bool:
    try:
        answer = input(
            f"Transfer ownership of {workbook_name!r}? [y/N] "
        ).strip().casefold()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workbook",
        help="One Sigma workbook URL or inode ID",
    )
    parser.add_argument(
        "--new-owner",
        help="New owner's exact email, member ID, or full name",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply in non-interactive use; otherwise prints the plan",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation; requires --execute",
    )
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--token-helper", type=Path, default=DEFAULT_TOKEN_HELPER)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.yes and not args.execute:
        raise SystemExit("ERROR: --yes requires --execute")

    workbook_ref = args.workbook or (
        prompt_workbook() if sys.stdin.isatty() else ""
    )
    if not workbook_ref:
        raise SystemExit("ERROR: provide one valid Sigma workbook URL or inode ID")

    load_env(args.env)
    token = mint_token(args.token_helper)
    base_url = os.environ.get("SIGMA_BASE_URL", "")
    if not base_url:
        raise SystemExit("ERROR: SIGMA_BASE_URL is not configured")
    api = SigmaApi(base_url, token)

    try:
        workbook = resolve_workbook_reference(api, workbook_ref)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    inode_id = str(workbook["id"])
    members = api.paginate(
        "/v2/members",
        {"limit": 1000, "includeArchived": "true", "includeInactive": "true"},
    )
    if args.new_owner:
        new_owner = resolve_member(members, args.new_owner)
    elif sys.stdin.isatty():
        new_owner = pick_new_owner(members)
        if not new_owner:
            print("Canceled.", file=sys.stderr)
            return 0
    else:
        raise SystemExit("ERROR: non-interactive use requires --new-owner")

    if new_owner.get("isArchived") or new_owner.get("isInactive"):
        raise SystemExit("ERROR: new owner must be an active Sigma member")

    current_owner_id = str(workbook.get("ownerId") or "")
    new_owner_id = str(new_owner["memberId"])
    if current_owner_id == new_owner_id:
        raise SystemExit("ERROR: selected member already owns this workbook")

    plan = {
        "workbook": {
            "id": inode_id,
            "name": workbook.get("name"),
            "url": workbook.get("url"),
        },
        "currentOwner": member_label(current_owner_id, members),
        "newOwner": member_display(new_owner),
    }
    print(json.dumps(plan, indent=2))

    interactive = sys.stdin.isatty()
    should_apply = args.execute and args.yes
    if interactive and not should_apply:
        should_apply = confirm_transfer(str(workbook.get("name") or inode_id))
    if not should_apply:
        print("Dry run — no ownership changed.", file=sys.stderr)
        return 0

    try:
        verified = reassign_workbook(api, inode_id, new_owner_id)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print(
        f"Transferred {verified.get('name') or inode_id} to "
        f"{member_display(new_owner)}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
