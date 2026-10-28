#!/usr/bin/env python3
"""Move selected Sigma people to another account type.

The account type is what Sigma assigns. Its license type is what gets billed,
so the picker shows that license in parentheses. Search by name, Tab to
select more than one, Enter to continue, then pick the account type.
"""

from __future__ import annotations

import sys
from typing import Any

from sigma_license_audit import SigmaClient, fzf_pick_choice, fzf_pick_multi


def account_type_label(name: str, tier: str) -> str:
    return f"{name} ({tier})"


def _tier(name: str, type_to_tier: dict[str, str]) -> str:
    if name in type_to_tier:
        return type_to_tier[name]
    folded = name.casefold()
    for key, tier in type_to_tier.items():
        if key.casefold() == folded:
            return tier
    return ""


def _labeled(name: str, type_to_tier: dict[str, str]) -> str:
    tier = _tier(name, type_to_tier)
    return account_type_label(name, tier) if tier else name


def person_name(member: dict[str, Any]) -> str:
    first = str(member.get("firstName") or "").strip()
    last = str(member.get("lastName") or "").strip()
    full = f"{first} {last}".strip()
    return full or str(member.get("email") or "").strip()


def selectable(member: dict[str, Any]) -> bool:
    if not str(member.get("memberId") or "").strip():
        return False
    if not str(member.get("email") or "").strip():
        return False
    if member.get("isArchived") or member.get("isInactive"):
        return False
    return str(member.get("userKind") or "").strip().casefold() != "embed"


def people_choices(members: list[dict[str, Any]], type_to_tier: dict[str, str]) -> list[tuple[str, str]]:
    people = [member for member in members if selectable(member)]
    people.sort(key=lambda member: (person_name(member).casefold(), str(member.get("email")).casefold()))
    name_width = max((len(person_name(member)) for member in people), default=0)
    email_width = max((len(str(member.get("email"))) for member in people), default=0)
    rows: list[tuple[str, str]] = []
    for member in people:
        email = str(member.get("email") or "")
        current = _labeled(str(member.get("memberType") or "").strip(), type_to_tier)
        label = f"{person_name(member):<{name_width}}  {email:<{email_width}}  {current}"
        rows.append((str(member["memberId"]), label))
    return rows


def account_type_choices(type_to_tier: dict[str, str]) -> list[tuple[str, str]]:
    names = sorted(type_to_tier, key=str.casefold)
    return [(name, account_type_label(name, type_to_tier[name])) for name in names]


def _by_id(members: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(member.get("memberId")): member for member in members if member.get("memberId")}


def changing(members: list[dict[str, Any]], selected_ids: list[str], target: str) -> list[dict[str, Any]]:
    found = _by_id(members)
    wanted = target.casefold()
    out: list[dict[str, Any]] = []
    for member_id in selected_ids:
        member = found.get(member_id)
        if member is None:
            continue
        current = str(member.get("memberType") or "").strip()
        if current.casefold() == wanted:
            continue
        out.append(member)
    return out


def proposal(members: list[dict[str, Any]], target: str, type_to_tier: dict[str, str]) -> str:
    target_label = _labeled(target, type_to_tier)
    count = len(members)
    noun = "1 person" if count == 1 else f"{count} people"
    lines = [f"Move {noun} to {target_label}", ""]
    ordered = sorted(members, key=lambda member: (person_name(member).casefold(), str(member.get("email")).casefold()))
    for member in ordered:
        current = _labeled(str(member.get("memberType") or "").strip(), type_to_tier)
        lines.append(f"  {person_name(member)}  {member.get('email')}  {current} → {target_label}")
    return "\n".join(lines)


def run(
    client: SigmaClient,
    *,
    pick_multi,
    pick,
    input_fn=input,
    members: list[dict[str, Any]] | None = None,
    type_to_tier: dict[str, str] | None = None,
) -> int:
    if members is None:
        members = client.load_members()
    if type_to_tier is None:
        type_to_tier = client.load_account_types_map()
    selected = pick_multi(
        people_choices(members, type_to_tier),
        "Type a name · Tab selects · Enter continues",
    )
    if not selected:
        print("No one selected.")
        return 0
    target = pick(
        account_type_choices(type_to_tier),
        "Account type (license type)",
    )
    if not target:
        print("Cancelled.")
        return 0
    moving = changing(members, selected, target)
    if not moving:
        print(f"Already on {_labeled(target, type_to_tier)}. Nothing to change.")
        return 0
    print(proposal(moving, target, type_to_tier))
    answer = input_fn(f"\nApply this for {len(moving)} people? [y/N] ").strip().casefold()
    if answer not in {"y", "yes"}:
        print("Cancelled.")
        return 0
    for member in moving:
        client.update_member_type(str(member["memberId"]), target)
        print(f"  updated {member.get('email')}")
    print(f"Updated {len(moving)} account type(s) to {target}.")
    return 0


def main() -> int:
    if not sys.stdin.isatty():
        print("Update license needs a terminal.", file=sys.stderr)
        return 1
    client = SigmaClient.from_env()
    return run(
        client,
        pick_multi=lambda entries, header: fzf_pick_multi(
            entries,
            header=header,
            prompt="people > ",
            height="80%",
            clear_query_on_toggle=True,
        ),
        pick=lambda entries, header: fzf_pick_choice(
            entries,
            header=header,
            prompt="account type > ",
            height="40%",
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
