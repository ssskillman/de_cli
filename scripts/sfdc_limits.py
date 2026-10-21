#!/usr/bin/env python3
"""Salesforce org limits: max and remaining allocations.

Uses the same SF CLI org as opp-formula-refresh (default alias iterable-prod).
GET /services/data/v{apiVersion}/limits
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from typing import Any

DEFAULT_ORG = "iterable-prod"
DEFAULT_API_VERSION = "67.0"


def limit_rows(payload: dict[str, Any]) -> list[tuple[str, int, int]]:
    rows: list[tuple[str, int, int]] = []
    for name, value in payload.items():
        if not isinstance(value, dict) or "Max" not in value or "Remaining" not in value:
            continue
        rows.append((str(name), int(value["Max"]), int(value["Remaining"])))
    rows.sort(key=lambda row: ((row[2] / row[1]) if row[1] else 0.0, row[0].casefold()))
    return rows


def filter_rows(rows: list[tuple[str, int, int]], name: str | None) -> list[tuple[str, int, int]]:
    needle = (name or "").casefold()
    if not needle:
        return rows
    return [row for row in rows if needle in row[0].casefold()]


def render_table(rows: list[tuple[str, int, int]]) -> str:
    headers = ("Limit", "Max", "Remaining", "Used")
    body = [
        (name, str(maximum), str(remaining), str(maximum - remaining))
        for name, maximum, remaining in rows
    ]
    widths = [len(header) for header in headers]
    for cells in body:
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], len(cell))

    def line(cells: tuple[str, ...], numeric: tuple[bool, ...]) -> str:
        parts = [
            cell.rjust(widths[index]) if numeric[index] else cell.ljust(widths[index])
            for index, cell in enumerate(cells)
        ]
        return "  ".join(parts)

    numeric = (False, True, True, True)
    lines = [
        line(headers, numeric),
        line(tuple("-" * width for width in widths), (False, False, False, False)),
    ]
    lines.extend(line(cells, numeric) for cells in body)
    return "\n".join(lines)


def api_version(org: str) -> str:
    proc = subprocess.run(
        ["sf", "org", "display", "--target-org", org, "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            f"sf org display failed for {org!r}. "
            f"Re-auth with `sf org login web --alias {org}`.\n{detail}"
        )
    payload = json.loads(proc.stdout)
    version = str((payload.get("result") or {}).get("apiVersion") or DEFAULT_API_VERSION)
    return version


def fetch_limits(org: str, version: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "sf",
            "api",
            "request",
            "rest",
            f"services/data/v{version}/limits",
            "--target-org",
            org,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip()
        raise RuntimeError(f"Salesforce limits request failed for {org!r}.\n{detail}")
    payload = json.loads(proc.stdout)
    if not isinstance(payload, dict):
        raise TypeError("Salesforce limits response was not a JSON object")
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Show Salesforce max and remaining allocations "
            f"(SF CLI org, default {DEFAULT_ORG})."
        )
    )
    parser.add_argument(
        "--org",
        default=DEFAULT_ORG,
        help=f"SF CLI org alias (default: {DEFAULT_ORG})",
    )
    parser.add_argument(
        "--api-version",
        default="",
        help="REST API version (default: the org's apiVersion from sf org display)",
    )
    parser.add_argument(
        "--name",
        default="",
        help="Only limits whose name contains this text",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw limits JSON",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if shutil.which("sf") is None:
        print("sf CLI is not on PATH. Install Salesforce CLI and `sf org login web`.", file=sys.stderr)
        return 1
    try:
        version = args.api_version.strip() or api_version(args.org)
        payload = fetch_limits(args.org, version)
    except (RuntimeError, TypeError, json.JSONDecodeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    rows = filter_rows(limit_rows(payload), args.name)
    print(f"Salesforce limits  org={args.org}  v{version}  {len(rows)} shown", file=sys.stderr)
    if not rows:
        print("No matching limits.", file=sys.stderr)
        return 1
    print(render_table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
