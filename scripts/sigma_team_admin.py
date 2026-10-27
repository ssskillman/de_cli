#!/usr/bin/env python3
"""Create a Sigma team, add people to one, or change licenses for one team.

The team command first asks whether to create a new team or add people to a
team that already exists. People come from Sam Allen's direct reports. A
leader can add chosen people one level down, or that leader and everyone
in the reporting tree.

Team license management changes only members of the selected team whose
current account type is the From type. Everyone else is left as they are.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sigma_license_audit import (
    ReportingWorker,
    SigmaClient,
    SnowflakeWorkerStore,
    ascii_table,
    fzf_pick_choice,
    fzf_pick_multi,
    fzf_pick_tree_action,
    manager_direct_reports,
    rebase_subtree,
    reporting_level_choices,
    reporting_tree_emails,
)

SAM_ALLEN_EMAIL = "sam.allen@iterable.com"
Pick = Callable[[list[tuple[str, str]], str], str | None]
PickMulti = Callable[[list[tuple[str, str]], str], list[str]]
TEAM_ACTION_HEADER = "Create a new team, or add people to one?"


def udi_reexec_python(*, snowflake_present: bool, already_reexeced: bool, udi_python: Path) -> Path | None:
    """ADP queries run in the UDI virtualenv, same as the other Sigma tools."""
    if already_reexeced or snowflake_present:
        return None
    if udi_python.is_file() and os.access(udi_python, os.X_OK):
        return udi_python
    return None


def _maybe_reexec_into_udi_venv() -> None:
    try:
        import snowflake.connector  # noqa: F401
        present = True
    except ImportError:
        present = False
    target = udi_reexec_python(
        snowflake_present=present,
        already_reexeced=os.environ.get("_SIGMA_AUDIT_REEXEC") == "1",
        udi_python=Path.home() / "UDI" / ".venv" / "bin" / "python",
    )
    if target is None:
        return
    env = dict(os.environ)
    env["_SIGMA_AUDIT_REEXEC"] = "1"
    os.execve(str(target), [str(target), *sys.argv], env)


def members_on_license(members: list[dict[str, Any]], from_type: str) -> list[dict[str, Any]]:
    """Team members whose account type is exactly the From license."""
    wanted = from_type.strip().casefold()
    return [
        member
        for member in members
        if str(member.get("memberType") or "").strip().casefold() == wanted
    ]


def whole_team_emails(workers: list[ReportingWorker], lead_email: str) -> list[str]:
    """The lead plus everyone who reports up to them."""
    return sorted(reporting_tree_emails(workers, lead_email))


def _worker(workers: list[ReportingWorker], email: str) -> ReportingWorker | None:
    wanted = email.casefold()
    return next((worker for worker in workers if worker.email.casefold() == wanted), None)


def _name_column_width(workers: list[ReportingWorker]) -> int:
    return max((len(worker.full_name) for worker in workers), default=len("name"))


def _choices(workers: list[ReportingWorker]) -> list[tuple[str, str]]:
    """Name and title in fixed columns, same layout as the de menu."""
    width = max(_name_column_width(workers), len("name"))
    rows: list[tuple[str, str]] = []
    for worker in workers:
        title = worker.job_title.strip()
        label = f"{worker.full_name:<{width}}  {title}" if title else worker.full_name
        rows.append((worker.email, label))
    return rows


def people_list_header(title: str, workers: list[ReportingWorker]) -> str:
    width = max(_name_column_width(workers), len("name"))
    return f"{title}\n{'name':<{width}}  title"


def _people(count: int) -> str:
    return "1 person" if count == 1 else f"{count} people"


def leader_add_choices(
    leader: ReportingWorker,
    workers: list[ReportingWorker],
) -> tuple[str, list[tuple[str, str]]]:
    """Two ways to add a leader's organization, with the counts in the labels."""
    directs = manager_direct_reports(workers, leader.email)
    everyone = whole_team_emails(workers, leader.email)
    return (
        f"{leader.full_name}: who should be added?",
        [
            ("teams", f"Choose from {_people(len(directs))} one level down"),
            ("whole", f"Add all {_people(len(everyone))}, including {leader.full_name}"),
        ],
    )


def reporting_count_section(
    workers: list[ReportingWorker],
    leader: ReportingWorker,
    included: set[str],
    *,
    team_name: str,
    excluded_names: list[str],
    included_names: list[str],
) -> str:
    """Direct, indirect, and total counts for the people currently included."""
    selected = [
        worker
        for worker in rebase_subtree(workers, leader.email)
        if worker.email.casefold() in included
    ]
    direct = [worker for worker in selected if worker.depth == 1]
    indirect = [worker for worker in selected if worker.depth != 1]
    table = ascii_table(
        ["Group", "People"],
        [
            ["Direct", str(len(direct))],
            ["Indirect", str(len(indirect))],
            ["Total", str(len(selected))],
        ],
        numeric_columns={1},
    )
    excluded = " · ".join(excluded_names)
    included_line = " · ".join(included_names)
    return (
        f"Excluded: {excluded}\n"
        f"Included: {included_line}\n\n"
        f"{team_name}\n"
        f"{leader.full_name}\n\n"
        f"People\n{table}"
    )


def select_people_one_level_down(
    workers: list[ReportingWorker],
    leader: ReportingWorker,
    *,
    team_name: str,
    pick_level=None,
    input_fn=input,
) -> list[str]:
    """Include or exclude reporting trees, then confirm the count for this team."""
    tree = rebase_subtree(workers, leader.email)
    if not tree:
        print(f"No people under {leader.full_name}.")
        return []
    picker = pick_level or fzf_pick_tree_action
    included = {worker.email.casefold() for worker in tree}
    by_email = {worker.email.casefold(): worker for worker in tree}
    stack = [leader.email.casefold()]
    names = {leader.email.casefold(): leader.full_name}
    excluded_names: list[str] = []
    included_names: list[str] = []

    def remember(name: str, *, include: bool) -> None:
        source = excluded_names if include else included_names
        target = included_names if include else excluded_names
        if name in source:
            source.remove(name)
        if name not in target:
            target.append(name)

    def show_counts() -> None:
        print(
            "\n"
            + reporting_count_section(
                workers,
                leader,
                included,
                team_name=team_name,
                excluded_names=excluded_names,
                included_names=included_names,
            )
        )

    while True:
        current_email = stack[-1]
        directs = manager_direct_reports(workers, current_email)
        if not directs:
            if len(stack) > 1:
                stack.pop()
                continue
            break
        crumb = " > ".join(names[email] for email in stack)
        action, email = picker(
            reporting_level_choices(directs, workers, included),
            prompt="teams > ",
            header=(
                f"{team_name} · {crumb} · their directs · Right=open their team · Left=back · "
                "Ctrl-Y=include tree · Ctrl-X=exclude tree · Enter=confirm"
            ),
        )
        action = (action or "").strip().casefold()
        chosen = by_email.get((email or "").casefold())
        if action in {"", "done", "enter"}:
            break
        if action in {"cancel", "esc"}:
            print("Cancelled.")
            return []
        if action in {"back", "left"}:
            if len(stack) > 1:
                stack.pop()
            continue
        if chosen is None:
            continue
        if action in {"drill", "right"}:
            if manager_direct_reports(workers, chosen.email):
                names[chosen.email.casefold()] = chosen.full_name
                stack.append(chosen.email.casefold())
            continue
        if action not in {"include", "exclude"}:
            continue
        emails = reporting_tree_emails(workers, chosen.email)
        if action == "include":
            included |= emails
        else:
            included -= emails
        remember(chosen.full_name, include=action == "include")
        show_counts()

    included -= {leader.email.casefold()}
    if not included:
        print("No people selected.")
        return []
    show_counts()
    answer = input_fn(f"\nAdd {_people(len(included))} to {team_name}? [y/N] ").strip().casefold()
    if answer not in {"y", "yes"}:
        print("Cancelled.")
        return []
    return sorted(included)


def gather_member_emails(
    workers: list[ReportingWorker],
    directs: list[ReportingWorker],
    *,
    pick: Pick,
    pick_multi: PickMulti | None = None,
    pick_level=None,
    team_name: str = "",
    input_fn=input,
) -> list[str]:
    """Walk Sam's directs. A leader adds selected people, or the whole tree."""
    chosen: list[str] = []
    seen: set[str] = set()

    def add(emails: list[str]) -> None:
        for email in emails:
            key = email.casefold()
            if key not in seen:
                seen.add(key)
                chosen.append(key)

    def take_leader(leader: ReportingWorker) -> None:
        header, choices = leader_add_choices(leader, workers)
        mode = pick(choices, header)
        if mode == "whole":
            add(whole_team_emails(workers, leader.email))
            return
        if mode != "teams":
            return
        add(
            select_people_one_level_down(
                workers,
                leader,
                team_name=team_name,
                pick_level=pick_level,
                input_fn=input_fn,
            )
        )

    while True:
        key = pick(
            [*_choices(directs), ("__done__", "Done")],
            people_list_header("Add someone from Sam Allen's directs", directs),
        )
        if key in {None, "__done__"}:
            break
        person = _worker(workers, key)
        if person is None:
            continue
        if manager_direct_reports(workers, person.email):
            take_leader(person)
        else:
            add([person.email])
    return chosen


def _count_for(counts: dict[str, int], name: str) -> int:
    wanted = name.casefold()
    return sum(count for key, count in counts.items() if key.casefold() == wanted)


def roster_member_id(row: dict[str, Any]) -> str:
    """Team membership rows use userId. Member records use memberId. The values match."""
    return str(row.get("memberId") or row.get("userId") or "")


def members_from_roster(
    roster: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        by_id[member_id]
        for row in roster
        if (member_id := roster_member_id(row)) in by_id
    ]


def license_counts(members: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for member in members:
        name = str(member.get("memberType") or "").strip() or "(none)"
        counts[name] = counts.get(name, 0) + 1
    return counts


def with_all_license_types(counts: dict[str, int], type_to_tier: dict[str, str]) -> dict[str, int]:
    """Every account type, including ones nobody on the team has."""
    canonical = {name.casefold(): name for name in type_to_tier}
    full = {name: 0 for name in type_to_tier}
    for key, count in counts.items():
        name = canonical.get(key.casefold(), key)
        full[name] = full.get(name, 0) + count
    return full


def counts_after_move(before: dict[str, int], from_type: str, to_type: str) -> dict[str, int]:
    """Move everyone on the From license to the To license. Other counts stay."""
    after = dict(before)
    from_key = next((key for key in after if key.casefold() == from_type.casefold()), from_type)
    to_key = next((key for key in after if key.casefold() == to_type.casefold()), to_type)
    moving = after.get(from_key, 0)
    after[from_key] = 0
    if from_key.casefold() != to_key.casefold():
        after[to_key] = after.get(to_key, 0) + moving
    else:
        after[to_key] = moving
    return after


def license_pivot(before: dict[str, int], after: dict[str, int] | None = None) -> str:
    names = sorted(set(before) | set(after or {}), key=str.casefold)
    if after is None:
        rows = [[name, str(before.get(name, 0))] for name in names]
        return ascii_table(["License", "People"], rows, numeric_columns={1})
    rows = [
        [name, str(before.get(name, 0)), str(after.get(name, 0))]
        for name in names
    ]
    return ascii_table(["License", "Before", "After"], rows, numeric_columns={1, 2})


def account_type_choices(
    type_to_tier: dict[str, str],
    counts: dict[str, int] | None = None,
) -> list[tuple[str, str]]:
    items = sorted(type_to_tier.items(), key=lambda item: item[0].casefold())
    name_width = max((len(name) for name, _tier in items), default=0)
    tier_width = max((len(tier) for _name, tier in items), default=0)
    return [
        (
            name,
            f"{name:<{name_width}}  {tier:<{tier_width}}  {_count_for(counts or {}, name)}",
        )
        for name, tier in items
    ]


def _paginate(client: SigmaClient, path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = None
    while True:
        params: dict[str, Any] = {"limit": 200}
        if page:
            params["page"] = page
        data = client.request("GET", path, params=params)
        entries = data.get("entries", []) if isinstance(data, dict) else []
        rows.extend(entry for entry in entries if isinstance(entry, dict))
        page = data.get("nextPage") if isinstance(data, dict) else None
        if not page:
            return rows


def _member_indexes(
    members: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_email: dict[str, dict[str, Any]] = {}
    for member in members:
        member_id = str(member.get("memberId") or "")
        email = str(member.get("email") or "").strip().casefold()
        if member_id:
            by_id[member_id] = member
        if email:
            by_email[email] = member
    return by_id, by_email


def _active_member_id(member: dict[str, Any] | None) -> str | None:
    if not member or member.get("isArchived") or member.get("isInactive"):
        return None
    member_id = str(member.get("memberId") or "")
    return member_id or None


def create_team(client: SigmaClient, name: str) -> dict[str, Any]:
    created = client.request(
        "POST",
        "/v2/teams",
        body={
            "name": name,
            "createTeamFolder": True,
            "visibility": "public",
        },
    )
    if not isinstance(created, dict) or not created.get("teamId"):
        raise RuntimeError(f"Sigma did not return a team id for {name!r}.")
    return created


SIGMA_TEAM_MEMBER_BATCH = 100


def add_team_members(client: SigmaClient, team_id: str, member_ids: list[str]) -> None:
    """Sigma accepts at most 100 adds or removes on one team-member request."""
    if not member_ids:
        return
    total = len(member_ids)
    for start in range(0, total, SIGMA_TEAM_MEMBER_BATCH):
        batch = member_ids[start : start + SIGMA_TEAM_MEMBER_BATCH]
        if total > SIGMA_TEAM_MEMBER_BATCH:
            print(f"Adding {start + 1}–{start + len(batch)} of {total}...")
        try:
            client.request(
                "PATCH",
                f"/v2/teams/{team_id}/members",
                body={"add": batch},
            )
        except Exception as exc:
            if start:
                raise RuntimeError(
                    f"{start} member(s) were added before this batch failed. {exc}"
                ) from exc
            raise


def _named_teams(client: SigmaClient) -> list[dict[str, Any]]:
    teams = [
        team
        for team in _paginate(client, "/v2/teams")
        if team.get("teamId") and str(team.get("name") or "").strip()
    ]
    teams.sort(key=lambda team: str(team.get("name")).casefold())
    return teams


def run_create(client: SigmaClient, *, input_fn=input, pick: Pick | None = None, pick_multi: PickMulti | None = None) -> int:
    if not sys.stdin.isatty():
        print("Create team needs a terminal.", file=sys.stderr)
        return 1
    chooser = pick or (lambda entries, header: fzf_pick_choice(entries, header=header, prompt="choice > "))
    multi = pick_multi or (
        lambda entries, header: fzf_pick_multi(entries, header=header, prompt="teams > ")
    )
    action = chooser(
        [("new", "Create a new team"), ("add", "Add people to a team")],
        TEAM_ACTION_HEADER,
    )
    if action == "new":
        name = input_fn("Team name: ").strip()
        if not name:
            print("No team name. Cancelled.")
            return 0
        created = create_team(client, name)
        team_id = str(created["teamId"])
        print(f"Created team {name} ({team_id}) with a team folder.")
    elif action == "add":
        teams = _named_teams(client)
        team_id = chooser(
            [(str(team["teamId"]), str(team["name"])) for team in teams],
            "Add people to which team?",
        )
        if not team_id:
            print("No team selected.")
            return 0
        name = next(str(team["name"]) for team in teams if str(team["teamId"]) == team_id)
        print(f"Adding people to {name}.")
    else:
        print("Cancelled.")
        return 0

    workers = SnowflakeWorkerStore.fetch_reporting_tree(SAM_ALLEN_EMAIL)
    directs = manager_direct_reports(workers, SAM_ALLEN_EMAIL)
    if not directs:
        print(f"No direct reports found for {SAM_ALLEN_EMAIL}.")
        return 0
    emails = gather_member_emails(
        workers,
        directs,
        pick=chooser,
        pick_multi=multi,
        team_name=name,
        input_fn=input_fn,
    )
    _by_id, by_email = _member_indexes(client.load_members())
    member_ids: list[str] = []
    missing: list[str] = []
    for email in emails:
        member_id = _active_member_id(by_email.get(email))
        if member_id:
            member_ids.append(member_id)
        else:
            missing.append(email)
    add_team_members(client, team_id, member_ids)
    print(f"Added {len(member_ids)} member(s) to {name}.")
    if missing:
        print("No active Sigma account, left off the team:")
        for email in missing:
            print(f"  {email}")
    return 0


def run_licenses(client: SigmaClient, *, input_fn=input, pick: Pick | None = None) -> int:
    if not sys.stdin.isatty():
        print("Team license management needs a terminal.", file=sys.stderr)
        return 1
    chooser = pick or (lambda entries, header: fzf_pick_choice(entries, header=header, prompt="license > "))
    teams = _named_teams(client)
    team_key = chooser(
        [(str(team["teamId"]), str(team["name"])) for team in teams],
        "Team",
    )
    if not team_key:
        print("No team selected.")
        return 0
    team = next(team for team in teams if str(team["teamId"]) == team_key)
    roster = _paginate(client, f"/v2/teams/{team_key}/members")
    by_id, _by_email = _member_indexes(client.load_members())
    on_team = members_from_roster(roster, by_id)
    type_to_tier = client.load_account_types_map()
    before = with_all_license_types(license_counts(on_team), type_to_tier)
    print(f"\n{team['name']}\n{license_pivot(before)}")
    answer = input_fn(f"\nChange licenses on {team['name']}? [y/N] ").strip().casefold()
    if answer not in {"y", "yes"}:
        print("No license changes.")
        return 0
    choices = account_type_choices(type_to_tier, before)
    from_type = chooser(choices, f"{team['name']}: From license")
    if not from_type:
        print("Cancelled.")
        return 0
    moving = _count_for(before, from_type)
    to_type = chooser(choices, f"{team['name']}: To license  ·  From {from_type} ({moving})")
    if not to_type:
        print("Cancelled.")
        return 0
    if from_type.casefold() == to_type.casefold():
        print("From and To are the same license. Nothing to change.")
        return 0

    after = counts_after_move(before, from_type, to_type)
    changing = members_on_license(on_team, from_type)
    print(
        f"\n{team['name']}: {from_type} → {to_type} for {len(changing)} people. "
        "Other licenses on this team stay. Other teams stay.\n"
        f"{license_pivot(before, after)}"
    )
    confirm = input_fn(
        f"\nApply {from_type} → {to_type} for {len(changing)} people on {team['name']}? [y/N] "
    ).strip().casefold()
    if confirm not in {"y", "yes"}:
        print("Cancelled.")
        return 0
    for member in changing:
        client.update_member_type(str(member["memberId"]), to_type)
        print(f"  updated {member.get('email')}")
    print(f"Updated {len(changing)} license(s). Left everyone else unchanged.")
    return 0


def main(argv: list[str] | None = None) -> int:
    _maybe_reexec_into_udi_venv()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in {"create", "licenses"}:
        print("Use create or licenses.", file=sys.stderr)
        return 2
    client = SigmaClient.from_env()
    if args[0] == "create":
        return run_create(client)
    return run_licenses(client)


if __name__ == "__main__":
    raise SystemExit(main())
