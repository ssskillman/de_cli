#!/usr/bin/env python3
"""Search Sigma workbooks by name. * and ? are wildcards."""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sigma_move_workbooks import (
    DEFAULT_ENV,
    DEFAULT_TOKEN_HELPER,
    SigmaApi,
    eprint,
    load_env,
    mint_token,
)


def name_matches(name: str, pattern: str) -> bool:
    """Case-insensitive name match. Plain text matches anywhere; * and ? are wildcards."""
    raw = pattern.strip()
    if not raw:
        raise ValueError("workbook name pattern is empty")
    compiled = raw
    if not any(char in raw for char in "*?"):
        compiled = "*" + raw.replace("[", "[[]") + "*"
    return fnmatch.fnmatchcase(name.casefold(), compiled.casefold())


def search_workbooks(rows: list[dict[str, Any]], pattern: str) -> list[dict[str, Any]]:
    found = [
        row
        for row in rows
        if name_matches(str(row.get("name") or ""), pattern)
    ]
    return sorted(found, key=lambda row: str(row.get("name") or "").casefold())


def list_workbooks(api: SigmaApi, *, include_archived: bool) -> list[dict[str, Any]]:
    archived_flags = (False, True) if include_archived else (False,)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for archived in archived_flags:
        params = {
            "limit": 1000,
            "skipPermissionCheck": "true",
            "isArchived": str(archived).lower(),
        }
        try:
            page = api.paginate("/v2/workbooks", params)
        except RuntimeError as exc:
            if "(403)" not in str(exc):
                raise
            params.pop("skipPermissionCheck")
            eprint("skipPermissionCheck denied; listing workbooks this token can see.")
            page = api.paginate("/v2/workbooks", params)
        for row in page:
            workbook_id = str(row.get("workbookId") or row.get("id") or "")
            if workbook_id and workbook_id in seen:
                continue
            if workbook_id:
                seen.add(workbook_id)
            rows.append(row)
    return rows


def format_matches(rows: list[dict[str, Any]]) -> str:
    lines = ["name\tworkbookId\turlId"]
    for row in rows:
        lines.append(
            "\t".join(
                [
                    str(row.get("name") or ""),
                    str(row.get("workbookId") or row.get("id") or ""),
                    str(row.get("workbookUrlId") or row.get("urlId") or ""),
                ]
            )
        )
    return "\n".join(lines)


def prompt_pattern() -> str:
    try:
        pattern = input("Workbook name (* and ? wildcards): ").strip()
    except (EOFError, KeyboardInterrupt):
        eprint("\nCancelled.")
        raise SystemExit(1) from None
    if not pattern:
        eprint("ERROR: enter a workbook name or wildcard.")
        raise SystemExit(1)
    return pattern


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Search Sigma workbooks by name. * and ? are wildcards."
    )
    parser.add_argument(
        "pattern",
        nargs="?",
        help="Name or wildcard. Plain text matches anywhere in the name.",
    )
    parser.add_argument(
        "--include-archived",
        action="store_true",
        help="Also search archived workbooks.",
    )
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--token-helper", type=Path, default=DEFAULT_TOKEN_HELPER)
    args = parser.parse_args(argv)

    pattern = args.pattern.strip() if args.pattern else ""
    if not pattern:
        if not sys.stdin.isatty():
            eprint("ERROR: pass a workbook name or wildcard.")
            return 1
        pattern = prompt_pattern()

    load_env(args.env)
    base_url = os.environ.get("SIGMA_BASE_URL", "").rstrip("/")
    if not base_url:
        eprint("ERROR: SIGMA_BASE_URL is empty")
        return 1
    token = os.environ.get("SIGMA_API_TOKEN") or mint_token(args.token_helper)
    api = SigmaApi(base_url, token)
    eprint(f"Searching workbooks for {pattern!r} …")
    matches = search_workbooks(
        list_workbooks(api, include_archived=args.include_archived),
        pattern,
    )
    if not matches:
        eprint(f"No workbooks matched {pattern!r}.")
        return 1
    eprint(f"{len(matches)} workbook(s)")
    print(format_matches(matches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
