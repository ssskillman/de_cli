#!/usr/bin/env python3
"""List Act account-type users on a Build license who are not building.

The account type named Act is a Build license. Act+ is the true Act license.
The report matches the license-audit utilization table, then asks who to move.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

from sigma_license_audit import (
    ActivityStore,
    AuditReporter,
    MemberAuditRecord,
    SigmaClient,
    SnowflakeWorkerStore,
    build_audit_records,
    fzf_pick_multi,
    resolve_act_plus_account_type,
    wait_with_braille,
)

REPORT_TITLE = "Act Account Type Build License move to Act+ (True Act)"
NOT_BUILD_WORK = {"UNDERUTILIZED_BUILD", "DORMANT"}


def _maybe_reexec_into_udi_venv() -> None:
    if os.environ.get("_SIGMA_AUDIT_REEXEC") == "1":
        return
    try:
        import snowflake.connector  # noqa: F401
    except ImportError:
        udi_py = Path.home() / "UDI" / ".venv" / "bin" / "python"
        if udi_py.is_file() and os.access(udi_py, os.X_OK):
            env = dict(os.environ)
            env["_SIGMA_AUDIT_REEXEC"] = "1"
            os.execve(str(udi_py), [str(udi_py), *sys.argv], env)


def act_build_not_building(records: list[MemberAuditRecord]) -> list[MemberAuditRecord]:
    """Active Act account type on a Build license, with no build edits."""
    found = []
    for record in records:
        if record.member_type.casefold() != "act":
            continue
        if record.license_type != "Build":
            continue
        if record.sigma_status != "active" or record.is_departed:
            continue
        if record.utilization_status not in NOT_BUILD_WORK:
            continue
        found.append(replace(record, recommendation="Move to Act+ (True Act)"))
    return found


def report_text(records: list[MemberAuditRecord]) -> str:
    people = act_build_not_building(records)
    table = AuditReporter.render_table(people, utilization_mode=True)
    return (
        "============================================================\n"
        f"  {REPORT_TITLE}\n"
        "============================================================\n"
        "Account type Act is a Build license. Act+ is the true Act license.\n"
        "These people are on Act and are not doing build work (no workbook edits).\n"
        f"{'1 person' if len(people) == 1 else f'{len(people)} people'}\n\n"
        f"{table}"
    )


def _by_name(people: list[MemberAuditRecord]) -> list[MemberAuditRecord]:
    return sorted(people, key=lambda person: (person.full_name.casefold(), person.email.casefold()))


def _choice_rows(people: list[MemberAuditRecord]) -> list[tuple[str, str]]:
    ordered = _by_name(people)
    name_width = max((len(person.full_name) for person in ordered), default=0)
    util_width = max((len(person.utilization_status) for person in ordered), default=0)
    rows = []
    for person in ordered:
        label = (
            f"{person.full_name:<{name_width}}  "
            f"{person.utilization_status:<{util_width}}  "
            f"{person.email}"
        )
        rows.append((person.email, label))
    return rows


def proposal_text(chosen: list[MemberAuditRecord], act_plus: str) -> str:
    ordered = _by_name(chosen)
    name_width = max((len(person.full_name) for person in ordered), default=0)
    lines = [
        f"Proposed: Act (Build license) → {act_plus} (True Act)",
        f"{'1 person' if len(ordered) == 1 else f'{len(ordered)} people'}",
        "",
    ]
    lines.extend(f"  {person.full_name:<{name_width}}  {person.email}" for person in ordered)
    return "\n".join(lines)


def run_report(
    records: list[MemberAuditRecord],
    *,
    client: SigmaClient,
    pick_multi=None,
    input_fn=input,
    act_plus: str = "Act+",
) -> int:
    people = act_build_not_building(records)
    print(report_text(people))
    if not people:
        return 0
    chooser = pick_multi or (
        lambda entries, **kwargs: fzf_pick_multi(
            entries,
            header=kwargs.get("header", ""),
            prompt="move > ",
            clear_query_on_toggle=True,
        )
    )
    selected = chooser(
        _choice_rows(people),
        header=(
            f"{REPORT_TITLE}\n"
            "Tab adds another person · Ctrl-A selects all · Enter reviews the proposal · Esc cancels"
        ),
    )
    chosen = _by_name(
        [person for person in people if person.email.casefold() in {email.casefold() for email in selected}]
    )
    if not chosen:
        print("No one selected.")
        return 0
    print("\n" + proposal_text(chosen, act_plus))
    answer = input_fn(
        f"\nApply this move of {len(chosen)} from Act (Build license) to {act_plus} (True Act)? [y/N] "
    ).strip().casefold()
    if answer not in {"y", "yes"}:
        print("Cancelled.")
        return 0
    for person in chosen:
        client.update_member_type(person.member_id, act_plus)
        print(f"  updated {person.email}")
    print(f"Moved {len(chosen)} license(s) to {act_plus}.")
    return 0


def main() -> int:
    _maybe_reexec_into_udi_venv()
    try:
        client = SigmaClient.from_env()
    except Exception as exc:
        print(f"ERROR connecting to Sigma API: {exc}", file=sys.stderr)
        return 1
    try:
        type_to_tier = wait_with_braille("Loading Sigma account types...", client.load_account_types_map)
        raw_members = wait_with_braille("Loading Sigma members...", client.load_members)
    except Exception as exc:
        print(f"ERROR querying Sigma API: {exc}", file=sys.stderr)
        return 1
    try:
        departed = wait_with_braille(
            "Loading departed employees from ADP...",
            SnowflakeWorkerStore.fetch_from_snowflake,
        )
    except Exception as exc:
        print(f"warn: departed-employee lookup skipped ({exc})", file=sys.stderr)
        departed = []
    records = build_audit_records(
        raw_members,
        departed,
        type_to_tier,
        activities=ActivityStore.load_cached(),
    )
    act_plus = resolve_act_plus_account_type(type_to_tier)
    return run_report(records, client=client, act_plus=act_plus)


if __name__ == "__main__":
    raise SystemExit(main())
