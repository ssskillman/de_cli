#!/usr/bin/env python3
"""Create the Iterable Data & Analytics Sigma workspace and folders if missing."""

from __future__ import annotations

import argparse
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

WORKSPACE_NAME = "Iterable | Data & Analytics"

# (folder name, child folders). A slash in a name is part of that one folder.
TREE: tuple[tuple[str, tuple], ...] = (
    (
        "01 - Certified Data Models",
        (
            ("ARR & Revenue", ()),
            ("Customers & Accounts", ()),
            ("Pipeline & Forecasting", ()),
            ("Product & Usage", ()),
            ("Finance & Operations", ()),
        ),
    ),
    ("02 - Development & QA", ()),
    ("03 - Shared Workbooks", ()),
    (
        "04 - Department Analytics",
        (
            ("GTM / Sales / Marketing", ()),
            ("Customer Success", ()),
            ("Finance / Product / People", ()),
        ),
    ),
    ("99 - Archive", ()),
)


class WorkspaceError(RuntimeError):
    """The target tree cannot be created safely."""


def _named(rows: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    needle = name.casefold()
    return [row for row in rows if str(row.get("name") or "").casefold() == needle]


def _first_id(row: dict[str, Any], keys: tuple[str, ...], label: str) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    raise WorkspaceError(f"ERROR: {label} response has no id ({', '.join(keys)})")


def workspace_parent_id(row: dict[str, Any]) -> str:
    return _first_id(
        row,
        ("inodeId", "folderId", "rootInodeId", "id", "workspaceId"),
        "workspace",
    )


def folder_id(row: dict[str, Any]) -> str:
    return _first_id(row, ("id", "inodeId"), "folder")


def find_workspaces(api: SigmaApi, name: str) -> list[dict[str, Any]]:
    rows = api.paginate("/v2.1/workspaces", {"exactName": name, "limit": 100})
    return _named(rows, name)


def child_folders(api: SigmaApi, parent_id: str) -> list[dict[str, Any]]:
    return api.paginate(
        "/v2/files",
        {
            "parentId": parent_id,
            "typeFilters": "folder",
            "directChildFilter": "true",
            "limit": 1000,
        },
    )


def ensure_workspace(api: SigmaApi) -> tuple[str, str]:
    matches = find_workspaces(api, WORKSPACE_NAME)
    if len(matches) > 1:
        raise WorkspaceError(
            f"ERROR: {len(matches)} workspaces named {WORKSPACE_NAME!r}; not creating another"
        )
    if matches:
        return "exists", workspace_parent_id(matches[0])
    created = api.request(
        "POST",
        "/v2/workspaces",
        body={"name": WORKSPACE_NAME, "noDuplicates": True},
    )
    return "created", workspace_parent_id(created)


def ensure_folder(api: SigmaApi, parent_id: str, name: str, path: str) -> tuple[str, str]:
    matches = _named(child_folders(api, parent_id), name)
    if len(matches) > 1:
        raise WorkspaceError(
            f"ERROR: {len(matches)} folders named {name!r} under {path}; not creating another"
        )
    if matches:
        return "exists", folder_id(matches[0])
    created = api.request(
        "POST",
        "/v2/files",
        body={"type": "folder", "name": name, "parentId": parent_id},
    )
    return "created", folder_id(created)


def ensure_tree(api: SigmaApi) -> list[str]:
    """Create missing workspace and folders. Return status lines. Never updates existing nodes."""
    lines: list[str] = []
    status, parent_id = ensure_workspace(api)
    lines.append(f"{status}\t{WORKSPACE_NAME}")

    def walk(parent: str, nodes: tuple[tuple[str, tuple], ...], prefix: str) -> None:
        for name, children in nodes:
            path = f"{prefix} / {name}"
            folder_status, child_id = ensure_folder(api, parent, name, path)
            lines.append(f"{folder_status}\t{path}")
            walk(child_id, children, path)

    walk(parent_id, TREE, WORKSPACE_NAME)
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create the Iterable | Data & Analytics Sigma workspace and its folders "
            "when they are missing. Existing workspaces and folders are left unchanged."
        )
    )
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--token-helper", type=Path, default=DEFAULT_TOKEN_HELPER)
    args = parser.parse_args(argv)

    load_env(args.env)
    base_url = os.environ.get("SIGMA_BASE_URL", "").rstrip("/")
    if not base_url:
        eprint("ERROR: SIGMA_BASE_URL is empty")
        return 1
    token = os.environ.get("SIGMA_API_TOKEN") or mint_token(args.token_helper)
    api = SigmaApi(base_url, token)
    try:
        lines = ensure_tree(api)
    except WorkspaceError as exc:
        eprint(exc)
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
