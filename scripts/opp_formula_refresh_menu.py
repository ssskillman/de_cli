#!/usr/bin/env python3
"""Choose an Opportunity formula load, then run the bi-astronomer smoke script.

No user flags → fzf (numbered menu if fzf is missing). Explicit flags skip the
picker and are forwarded unchanged. ``--snowflake-user`` injected by ``de`` is
not a user flag.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

PROFILES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "open-write",
        "Daily — open Opps only → Snowflake MERGE (DAG-like, faster)",
        ("--open-only", "--write-snowflake", "--snowflake-auth", "externalbrowser"),
    ),
    (
        "full-write",
        "Full — all Opps (~40k) → Snowflake MERGE (bootstrap / weekly)",
        ("--full", "--write-snowflake", "--snowflake-auth", "externalbrowser"),
    ),
    (
        "open-smoke",
        "Smoke — open Opps, limit 10, no Snowflake",
        ("--open-only", "--limit", "10"),
    ),
    (
        "full-csv",
        "Extract only — full Opps, CSV stdout (no MERGE)",
        ("--full",),
    ),
    (
        "custom",
        "Custom — type flags manually (advanced)",
        (),
    ),
)


def split_snowflake_user(argv: list[str]) -> tuple[list[str], list[str]]:
    """Separate ``de``'s ``--snowflake-user`` injection from flags the user typed."""
    user: list[str] = []
    snow: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--snowflake-user" and index + 1 < len(argv):
            snow = ["--snowflake-user", argv[index + 1]]
            index += 2
            continue
        user.append(token)
        index += 1
    return user, snow


def resolve_args(
    argv: list[str],
    *,
    pick,
    custom_line,
) -> list[str] | None:
    """Return smoke-script args, or None when the user cancels."""
    user_args, snow_args = split_snowflake_user(argv)
    if user_args:
        return list(argv)

    key = pick()
    if key is None:
        return None
    if key == "custom":
        line = custom_line()
        if line is None or not str(line).strip():
            return None
        return [*shlex.split(str(line)), *snow_args]

    for profile_key, _label, args in PROFILES:
        if profile_key == key:
            return [*args, *snow_args]
    return None


def profile_key_for(args: list[str]) -> str:
    snow_free, _snow = split_snowflake_user(args)
    for key, _label, profile_args in PROFILES:
        if key != "custom" and tuple(snow_free) == profile_args:
            return key
    return "custom"


def smoke_script() -> Path:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from de_cli.config import load_config

    return load_config().require_path("bi_astronomer") / "scripts" / "run-opp-formula-refresh-local.sh"


def pick_profile() -> str | None:
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from de_cli.menu_pick import pick_key

    return pick_key(
        [(key, label) for key, label, _args in PROFILES],
        header="Opportunity formula refresh — pick load mode",
        prompt="load > ",
        list_title="Opportunity formula refresh",
        border_label="SFDC",
    )


def read_custom_line() -> str | None:
    try:
        return input("Flags (example: --open-only --limit 25): ")
    except (EOFError, KeyboardInterrupt):
        return None


def main(argv: list[str] | None = None) -> int:
    args = resolve_args(
        list(sys.argv[1:] if argv is None else argv),
        pick=pick_profile,
        custom_line=read_custom_line,
    )
    if args is None:
        return 0
    script = smoke_script()
    if not script.is_file():
        print(f"Missing smoke script: {script}", file=sys.stderr)
        return 1
    if not split_snowflake_user(list(sys.argv[1:] if argv is None else argv))[0]:
        shown = " ".join(split_snowflake_user(args)[0])
        print(
            f"opp-formula-refresh profile: {profile_key_for(args)} ({shown})",
            file=sys.stderr,
        )
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(script, [str(script), *args])
    except OSError as exc:
        print(f"Failed to run {script}: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
