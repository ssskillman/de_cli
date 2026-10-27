#!/usr/bin/env python3
"""Active Salesforce integration and API-only users, with password expiration.

Uses the same SF CLI org as the other Salesforce commands (default iterable-prod).
Read-only. Does not print tokens or passwords.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

DEFAULT_ORG = "iterable-prod"

USER_QUERY = """
SELECT Name, Username, Profile.Name, PasswordExpirationDate, LastLoginDate
FROM User
WHERE IsActive = true
AND (
  Profile.PermissionsApiUserOnly = true
  OR Profile.Name LIKE '%Integration%'
)
ORDER BY Name
""".strip()


def parse_sf_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if len(text) >= 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def expiration_status(expires: datetime | None, now: datetime) -> str:
    if expires is None:
        return "none"
    if expires <= now.astimezone(UTC):
        return "expired"
    return "active"


def user_rows(records: list[dict[str, Any]], now: datetime) -> list[dict[str, str]]:
    rows = []
    for record in records:
        profile = record.get("Profile") or {}
        expires = parse_sf_datetime(record.get("PasswordExpirationDate"))
        last_login = parse_sf_datetime(record.get("LastLoginDate"))
        status = expiration_status(expires, now)
        rows.append(
            {
                "user": str(record.get("Name") or ""),
                "username": str(record.get("Username") or ""),
                "profile": str(profile.get("Name") or ""),
                "expires": format_when(expires),
                "status": status,
                "last_login": format_when(last_login),
                "_expires": expires.isoformat() if expires else "",
            }
        )
    rows.sort(
        key=lambda row: (
            {"expired": 0, "active": 1, "none": 2}[row["status"]],
            row["_expires"] or "9999",
            row["user"].casefold(),
        )
    )
    return rows


def format_when(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def render_table(rows: list[dict[str, str]]) -> str:
    headers = ("User", "Username", "Profile", "Password expires", "Status", "Last login")
    keys = ("user", "username", "profile", "expires", "status", "last_login")
    body = [tuple(row[key] for key in keys) for row in rows]
    widths = [len(header) for header in headers]
    for cells in body:
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], len(cell))

    def line(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells))

    lines = [line(headers), line(tuple("-" * width for width in widths))]
    lines.extend(line(cells) for cells in body)
    return "\n".join(lines)


def query_users(org: str) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["sf", "data", "query", "--target-org", org, "--json", "--query", USER_QUERY],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Salesforce user query failed for {org!r}.\n{detail}")
    payload = json.loads(proc.stdout)
    result = payload.get("result") or {}
    records = list(result.get("records") or [])
    if result.get("done") is False:
        raise RuntimeError("Salesforce returned a partial user page. Narrow the query and retry.")
    return records


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "List active Salesforce integration and API-only users and their "
            f"password expiration (SF CLI org, default {DEFAULT_ORG})."
        )
    )
    parser.add_argument("--org", default=DEFAULT_ORG, help=f"SF CLI org alias (default: {DEFAULT_ORG})")
    parser.add_argument("--json", action="store_true", help="Print the rows as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if shutil.which("sf") is None:
        print("sf CLI is not on PATH. Install Salesforce CLI and `sf org login web`.", file=sys.stderr)
        return 1
    try:
        records = query_users(args.org)
        rows = user_rows(records, datetime.now(UTC))
    except (RuntimeError, json.JSONDecodeError, OSError, TypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        json.dump([{key: row[key] for key in row if not key.startswith("_")} for row in rows], sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    expired = [row for row in rows if row["status"] == "expired"]
    print(
        f"Salesforce API users  org={args.org}  {len(rows)} active  {len(expired)} password expired",
        file=sys.stderr,
    )
    if not rows:
        print("No active integration or API-only users.")
        return 0
    print(render_table(rows))
    if expired:
        print("\nExpired")
        print(render_table(expired))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
