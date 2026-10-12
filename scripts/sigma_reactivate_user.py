#!/usr/bin/env python3
"""Reactivate one Sigma user by email."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from sigma_license_audit import DEFAULT_SIGMA_ENV, SigmaClient  # noqa: E402


class ReactivateError(RuntimeError):
    """The member cannot be reactivated."""


def reactivation_body(member: dict[str, Any], force: bool = False) -> dict[str, bool]:
    """Return the member update body. Empty means the user is already active."""
    if member.get("isInactive") and not force:
        email = member.get("email") or member.get("memberId")
        raise ReactivateError(
            f"{email} is inactive from SCIM. Reactivate them in the IdP, or pass --force."
        )
    body: dict[str, bool] = {}
    if member.get("isArchived"):
        body["isArchived"] = False
    if member.get("isInactive"):
        body["isInactive"] = False
    return body


def find_member(members: list[dict[str, Any]], email: str) -> dict[str, Any]:
    needle = email.strip().casefold()
    if not needle:
        raise ReactivateError("email is required")
    matches = [
        member
        for member in members
        if str(member.get("email") or "").casefold() == needle
    ]
    if not matches:
        raise ReactivateError(f"no Sigma user with email {email}")
    if len(matches) > 1:
        raise ReactivateError(f"{email} matched {len(matches)} Sigma users")
    return matches[0]


def apply_reactivation(
    client: SigmaClient, member: dict[str, Any], body: dict[str, bool]
) -> str:
    member_id = str(member.get("memberId") or "")
    email = str(member.get("email") or member_id)
    if not member_id:
        raise ReactivateError(f"{email} has no memberId")
    updated = client.request("PATCH", f"/v2/members/{member_id}", body=body)
    if not isinstance(updated, dict):
        raise ReactivateError(f"Sigma did not return the updated member for {email}")
    if updated.get("isArchived") or updated.get("isInactive"):
        raise ReactivateError(f"{email} is still deactivated after the update")
    return f"reactivated {email} ({member_id})"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reactivate one Sigma user by email.")
    parser.add_argument("email", nargs="?", help="Email address of the user to reactivate.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Also clear SCIM inactive. The IdP may archive the user again.",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=DEFAULT_SIGMA_ENV,
        help=f"Sigma env file. Default: {DEFAULT_SIGMA_ENV}.",
    )
    return parser.parse_args(argv)


def prompt_email() -> str:
    try:
        email = input("Email: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise ReactivateError("email is required") from None
    if not email:
        raise ReactivateError("email is required")
    return email


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    email = (args.email or "").strip()
    if not email:
        if not sys.stdin.isatty():
            print("ERROR: pass an email address", file=sys.stderr)
            return 2
        email = prompt_email()

    client = SigmaClient.from_env(args.env)
    member = find_member(client.load_members(), email)
    body = reactivation_body(member, force=args.force)
    if not body:
        print(f"already active {member.get('email')} ({member.get('memberId')})")
        return 0
    print(apply_reactivation(client, member, body))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ReactivateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
