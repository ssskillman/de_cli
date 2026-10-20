#!/usr/bin/env python3
"""Deactivate Sigma member(s) and reassign documents via the Sigma CLI."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import sigma_user_downgrade_handoff as handoff

Member = handoff.Member
Sigma = handoff.Sigma
SigmaError = handoff.SigmaError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deactivate Sigma user(s) with the members update API (archive + "
            "document reassignment). Dry-run by default."
        )
    )
    parser.add_argument(
        "--user",
        dest="users",
        action="append",
        help="User email, member ID, or unique name match. Repeat for multiple users.",
    )
    parser.add_argument(
        "--to",
        "--new-owner",
        dest="new_owner",
        help="Active Sigma user who receives the deactivated user's documents.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Walk through choices in a menu (default when no users are supplied).",
    )
    parser.add_argument(
        "--archive-docs",
        action="store_true",
        help="Archive the user's documents instead of transferring to --new-owner.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow deactivating SCIM-provisioned users (prefer your IdP otherwise).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply deactivation. Default is dry-run.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation with --execute.",
    )
    parser.add_argument("--verbose", action="store_true", help="Print Sigma CLI calls.")
    parser.add_argument(
        "--profile",
        default=os.environ.get("SIGMA_PROFILE")
        or os.environ.get("DE_SIGMA_PROFILE")
        or "de",
        help="Sigma CLI profile. Default: SIGMA_PROFILE, DE_SIGMA_PROFILE, or de.",
    )
    parser.add_argument(
        "--sigma-bin",
        default=os.environ.get("SIGMA_BIN"),
        help="Path to the Sigma CLI binary.",
    )
    parser.add_argument(
        "--sigma-env",
        type=Path,
        default=Path(os.environ.get("SIGMA_ENV", handoff.DEFAULT_SIGMA_ENV)),
        help=f"Sigma env file. Default: {handoff.DEFAULT_SIGMA_ENV}.",
    )
    parser.add_argument(
        "--get-token",
        type=Path,
        default=Path(os.environ.get("GET_TOKEN_SH", handoff.DEFAULT_GET_TOKEN)),
        help=f"Token helper script. Default: {handoff.DEFAULT_GET_TOKEN}.",
    )
    parser.add_argument(
        "--bootstrap",
        type=Path,
        default=Path(os.environ.get("BOOTSTRAP_PY", handoff.DEFAULT_BOOTSTRAP)),
        help=f"Sigma CLI profile bootstrap. Default: {handoff.DEFAULT_BOOTSTRAP}.",
    )
    return parser.parse_args()


def member_looks_scim(raw: dict[str, Any]) -> bool:
    def norm(value: Any) -> str:
        return str(value or "").lower()

    if norm(raw.get("userKind")) == "scim":
        return True
    if norm(raw.get("provisioningMethod")) == "scim":
        return True
    if norm(raw.get("provisionedBy")) == "scim":
        return True
    if "scim" in norm(raw.get("identityProvider")):
        return True
    return raw.get("isScim") is True or raw.get("scim") is True


def load_members_with_raw(sigma: Sigma) -> tuple[list[Member], dict[str, dict[str, Any]]]:
    raw_members = handoff.list_all(
        sigma,
        "members",
        "list",
        extra={"includeArchived": True, "includeInactive": True},
    )
    by_id: dict[str, dict[str, Any]] = {}
    members: list[Member] = []
    for raw in raw_members:
        member = handoff.member_from_json(raw)
        if member.member_id and member.email:
            members.append(member)
            by_id[member.member_id] = raw
    return members, by_id


def interactive_setup(args: argparse.Namespace, members: list[Member]) -> None:
    print("\nSigma user deactivation")
    print("Select users to deactivate and who should receive their documents.")
    print("Nothing changes until you confirm.\n")

    selected: list[Member] = []
    while True:
        user = handoff.choose_member(
            "Find the user to deactivate",
            members,
            excluded_ids={member.member_id for member in selected},
        )
        selected.append(user)
        if not handoff.prompt_yes_no("Deactivate another user?"):
            break

    target = handoff.choose_member(
        "Find the user who should receive their documents",
        members,
        excluded_ids={member.member_id for member in selected},
    )

    args.archive_docs = handoff.prompt_yes_no(
        "Archive documents instead of transferring to the new owner?",
        default=False,
    )
    args.execute = (
        handoff.prompt_choice(
            "What should this run do?",
            ["Preview the plan only", "Apply deactivation after final confirmation"],
        )
        == 2
    )
    args.users = [member.email for member in selected]
    args.new_owner = target.email


def equivalent_command(args: argparse.Namespace) -> str:
    command = [str(Path(__file__).resolve())]
    for user in args.users or []:
        command.extend(["--user", user])
    if args.new_owner:
        command.extend(["--to", args.new_owner])
    if args.archive_docs:
        command.append("--archive-docs")
    if args.force:
        command.append("--force")
    if args.execute:
        command.append("--execute")
    return shlex.join(command)


def confirm_or_exit(args: argparse.Namespace) -> bool:
    if not args.execute:
        print("dry-run only; pass --execute to apply changes")
        return False
    if args.yes:
        return True
    if not sys.stdin.isatty():
        handoff.die("--execute requires --yes when stdin is not a TTY", 2)
    answer = input("Apply Sigma user deactivation? Type 'yes': ")
    if answer != "yes":
        raise SystemExit("aborted")
    return True


def build_plan(
    args: argparse.Namespace,
    members: list[Member],
    raw_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not args.users:
        handoff.die("at least one --user is required outside interactive mode", 2)
    if not args.new_owner:
        handoff.die("--to / --new-owner is required", 2)

    owner = handoff.resolve_member(args.new_owner, members, "destination")
    if owner.is_archived or owner.is_inactive:
        handoff.die(f"--new-owner must be an active user: {owner.email}")

    plan: list[dict[str, Any]] = []
    for query in args.users:
        source = handoff.resolve_member(query, members, "source")
        if source.member_id == owner.member_id:
            handoff.die("user to deactivate and document owner must be different")
        if source.is_archived or source.is_inactive:
            handoff.die(f"user is not active: {source.email}")
        raw = raw_by_id.get(source.member_id, {})
        if member_looks_scim(raw) and not args.force:
            handoff.die(
                f"refusing to deactivate SCIM-provisioned user {source.email} "
                "(use the IdP, or pass --force)"
            )
        plan.append(
            {
                "email": source.email,
                "memberId": source.member_id,
                "memberType": source.member_type,
                "newOwner": owner.email,
                "newOwnerId": owner.member_id,
                "archiveDocuments": bool(args.archive_docs),
                "action": "members.update",
                "looksScim": member_looks_scim(raw),
            }
        )
    return plan


def apply_plan(sigma: Sigma, plan: list[dict[str, Any]]) -> None:
    for row in plan:
        body: dict[str, Any] = {
            "isArchived": True,
            "newOwnerId": row["newOwnerId"],
        }
        if row.get("archiveDocuments"):
            body["archiveDocuments"] = True
        sigma.api(
            "members",
            "update",
            params={"memberId": row["memberId"]},
            body=body,
        )
        print(f"deactivated {row['email']} (documents → {row['newOwner']})")


def main() -> int:
    args = parse_args()
    interactive = args.interactive or (not args.users and not args.new_owner)
    if interactive and not sys.stdin.isatty():
        handoff.die(
            "interactive mode requires a terminal; supply --user and --to instead",
            2,
        )
    if not interactive and not args.users:
        handoff.die("at least one --user is required outside interactive mode", 2)

    os.environ["PATH"] = (
        f"{Path.home() / '.sigma-cli' / 'bin'}:/opt/homebrew/bin:/usr/local/bin:"
        f"{os.environ.get('PATH', '')}"
    )
    sigma_bin = handoff.resolve_sigma_bin(args.sigma_bin)

    handoff.load_env_file(args.sigma_env)
    for key in ("SIGMA_BASE_URL", "SIGMA_CLIENT_ID", "SIGMA_CLIENT_SECRET"):
        if not os.environ.get(key):
            handoff.die(f"{key} must be set in {args.sigma_env}")

    handoff.refresh_token(args.get_token)
    sigma = Sigma(
        sigma_bin=sigma_bin,
        profile=args.profile,
        get_token=args.get_token,
        verbose=args.verbose,
    )
    handoff.ensure_profile(args, sigma)
    sigma.api("whoami", "get")

    members, raw_by_id = load_members_with_raw(sigma)
    if interactive:
        interactive_setup(args, members)
        print("\nEquivalent command:")
        print(f"  {equivalent_command(args)}")

    plan = build_plan(args, members, raw_by_id)
    print("plan:")
    print(json.dumps(plan, indent=2, sort_keys=True))
    if not confirm_or_exit(args):
        return 0

    apply_plan(sigma, plan)
    print(f"deactivated {len(plan)} member(s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SigmaError as exc:
        handoff.die(str(exc))
