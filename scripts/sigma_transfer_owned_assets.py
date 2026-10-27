#!/usr/bin/env python3
"""Transfer every discoverable Sigma asset owned by one member to another."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_ENV = Path.home() / ".config" / "sigma" / "env"
DEFAULT_TOKEN_HELPER = (
    Path.home() / "github" / "sigma-skills" / "sigma-api" / "scripts" / "get-token.sh"
)


def die(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def load_env(path: Path) -> None:
    if not path.is_file():
        die(f"missing Sigma env file: {path}")
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        parts = shlex.split(value.strip())
        os.environ.setdefault(key, parts[0] if parts else "")


def mint_token(helper: Path) -> str:
    proc = subprocess.run(
        ["bash", str(helper)], capture_output=True, text=True, check=False
    )
    if proc.returncode:
        sys.stderr.write(proc.stderr)
        die("failed to mint Sigma API token")
    match = re.search(
        r"(?:export\s+)?SIGMA_API_TOKEN=(?:'([^']*)'|\"([^\"]*)\"|(\S+))",
        proc.stdout,
    )
    if not match:
        die("token helper did not print SIGMA_API_TOKEN")
    return next(value for value in match.groups() if value is not None)


class SigmaApi:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{query}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        def send() -> Any:
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"{method} {path} failed ({exc.code}): {detail}") from exc

        from sigma_license_audit import wait_with_braille

        payload = wait_with_braille(f"Sigma {method} {path}", send)
        if not payload:
            return None
        return json.loads(payload)

    def paginate(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page: str | None = None
        while True:
            query = dict(params)
            if page:
                query["page"] = page
            response = self.request("GET", path, query)
            rows.extend(response.get("entries") or [])
            page = response.get("nextPage")
            if not page:
                return rows


def member_matches(row: dict[str, Any], query: str) -> bool:
    needle = query.casefold()
    values = {
        str(row.get("memberId") or "").casefold(),
        str(row.get("email") or "").casefold(),
        f"{row.get('firstName') or ''} {row.get('lastName') or ''}".strip().casefold(),
    }
    return needle in values


def resolve_member(rows: list[dict[str, Any]], query: str) -> dict[str, Any]:
    matches = [row for row in rows if member_matches(row, query)]
    if len(matches) != 1:
        die(f"member {query!r} matched {len(matches)} users")
    return matches[0]


def owner_is_tom(row: dict[str, Any], tom: dict[str, Any]) -> bool:
    values = {
        str(row.get("ownerId") or "").casefold(),
        str(row.get("owner") or "").casefold(),
    }
    expected = {
        str(tom.get("memberId") or "").casefold(),
        str(tom.get("email") or "").casefold(),
        f"{tom.get('firstName') or ''} {tom.get('lastName') or ''}".strip().casefold(),
    }
    return bool(values & expected)


def add_candidate(
    candidates: dict[str, dict[str, Any]], row: dict[str, Any], id_fields: tuple[str, ...]
) -> None:
    inode_id = next((str(row.get(key)) for key in id_fields if row.get(key)), "")
    if not inode_id:
        return
    current = candidates.setdefault(inode_id, {"id": inode_id})
    for key in ("name", "type", "path", "ownerId", "isArchived", "url"):
        if row.get(key) is not None:
            current[key] = row[key]


def discover_owned_assets(
    api: SigmaApi, tom: dict[str, Any], destination_folder_id: str
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}

    member_id = str(tom["memberId"])
    member_files = api.paginate(
        f"/v2/members/{member_id}/files", {"limit": 1000}
    )
    for row in member_files:
        if owner_is_tom(row, tom):
            add_candidate(candidates, row, ("id",))

    for archived in (False, True):
        rows = api.paginate(
            "/v2/workbooks",
            {
                "limit": 1000,
                "skipPermissionCheck": "true",
                "isArchived": str(archived).lower(),
            },
        )
        for row in rows:
            if owner_is_tom(row, tom):
                row.setdefault("type", "workbook")
                add_candidate(candidates, row, ("workbookId",))

    data_models = api.paginate(
        "/v2/dataModels", {"limit": 1000, "skipPermissionCheck": "true"}
    )
    for row in data_models:
        if owner_is_tom(row, tom):
            row.setdefault("type", "data-model")
            add_candidate(candidates, row, ("dataModelId",))

    datasets = api.paginate(
        "/v2/datasets", {"limit": 1000, "skipPermissionCheck": "true"}
    )
    for row in datasets:
        if owner_is_tom(row, tom):
            row.setdefault("type", "dataset")
            add_candidate(candidates, row, ("datasetId",))

    excluded = {
        destination_folder_id,
        str(tom.get("homeFolderId") or ""),
    }
    verified: list[dict[str, Any]] = []
    for inode_id, candidate in candidates.items():
        if inode_id in excluded:
            continue
        try:
            current = api.request("GET", f"/v2/files/{inode_id}")
        except RuntimeError as exc:
            print(f"WARNING: cannot inspect {inode_id}: {exc}", file=sys.stderr)
            continue
        if str(current.get("ownerId") or "") != member_id:
            continue
        if current.get("type") in {"folder", "workspace", "symlink"}:
            continue
        current["id"] = inode_id
        current.setdefault("name", candidate.get("name"))
        current.setdefault("type", candidate.get("type"))
        verified.append(current)

    return sorted(verified, key=lambda row: (str(row.get("type")), str(row.get("name"))))


def restore_inode_and_archived_ancestors(
    api: SigmaApi, inode_id: str, visited: set[str] | None = None
) -> None:
    seen = visited or set()
    if inode_id in seen:
        raise RuntimeError(f"cycle while restoring archived inode {inode_id}")
    seen.add(inode_id)
    try:
        api.request("PATCH", f"/v2/files/{inode_id}", body={"restore": True})
        return
    except RuntimeError as exc:
        if "inode_archived" not in str(exc):
            raise
    current = api.request("GET", f"/v2/files/{inode_id}")
    parent_id = str(current.get("parentId") or "")
    if not parent_id:
        raise RuntimeError(f"cannot restore {inode_id}: archived ancestor not identified")
    restore_inode_and_archived_ancestors(api, parent_id, seen)
    api.request("PATCH", f"/v2/files/{inode_id}", body={"restore": True})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--destination-folder-id", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--owner-only",
        action="store_true",
        help="Change ownership for every asset without changing its parent folder.",
    )
    mode.add_argument(
        "--data-assets-owner-only",
        action="store_true",
        help=(
            "Keep datasets and data models in their current folders while changing "
            "their owner; move other document types to the destination folder."
        ),
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--token-helper", type=Path, default=DEFAULT_TOKEN_HELPER)
    args = parser.parse_args()

    load_env(args.env)
    token = mint_token(args.token_helper)
    base_url = os.environ.get("SIGMA_BASE_URL", "")
    if not base_url:
        die("SIGMA_BASE_URL is not configured")
    api = SigmaApi(base_url, token)

    members = api.paginate(
        "/v2/members",
        {"limit": 1000, "includeArchived": "true", "includeInactive": "true"},
    )
    source = resolve_member(members, args.source)
    destination = resolve_member(members, args.destination)
    folder = api.request("GET", f"/v2/files/{args.destination_folder_id}")
    if str(folder.get("ownerId") or "") != str(destination["memberId"]):
        die("destination folder is not owned by the destination member")
    if str(folder.get("name") or "") != "Tom Corbetts Documents":
        die(f"unexpected destination folder name: {folder.get('name')!r}")

    assets = discover_owned_assets(api, source, args.destination_folder_id)
    plan = {
        "source": {"name": "Tom Corbett", "memberId": source["memberId"]},
        "destination": {
            "name": "Justin Foulk",
            "memberId": destination["memberId"],
            "folderId": args.destination_folder_id,
            "folderName": folder.get("name"),
        },
        "count": len(assets),
        "assets": [
            {
                "id": row["id"],
                "name": row.get("name"),
                "type": row.get("type"),
                "path": row.get("path"),
                "ownerId": row.get("ownerId"),
                "action": (
                    "owner-only"
                    if args.owner_only
                    or (
                        args.data_assets_owner_only
                        and row.get("type") in {"dataset", "data-model"}
                    )
                    else "move-and-transfer-owner"
                ),
            }
            for row in assets
        ],
    }
    print(json.dumps(plan, indent=2, sort_keys=True))

    if not args.execute:
        print("dry-run only; no Sigma assets changed")
        return 0
    if not args.yes:
        die("--execute requires --yes")

    failures: list[dict[str, str]] = []
    moved = 0
    for asset in assets:
        inode_id = str(asset["id"])
        try:
            original_parent_id = str(asset.get("parentId") or "")
            keep_in_place = args.owner_only or (
                args.data_assets_owner_only
                and asset.get("type") in {"dataset", "data-model"}
            )
            transfer_body = {"ownerId": destination["memberId"]}
            if not keep_in_place:
                transfer_body["parentId"] = args.destination_folder_id
            try:
                api.request("PATCH", f"/v2/files/{inode_id}", body=transfer_body)
            except RuntimeError as exc:
                if "inode_archived" not in str(exc):
                    raise
                try:
                    restore_inode_and_archived_ancestors(api, inode_id)
                    api.request("PATCH", f"/v2/files/{inode_id}", body=transfer_body)
                except RuntimeError as restore_exc:
                    if "can only unarchive" not in str(restore_exc):
                        raise
                    api.request(
                        "PATCH",
                        f"/v2/files/{inode_id}",
                        body={**transfer_body, "restore": True},
                    )
            verified = api.request("GET", f"/v2/files/{inode_id}")
            owner_ok = str(verified.get("ownerId") or "") == str(destination["memberId"])
            expected_parent_id = (
                original_parent_id if keep_in_place else args.destination_folder_id
            )
            parent_ok = str(verified.get("parentId") or "") == expected_parent_id
            if not owner_ok or not parent_ok:
                raise RuntimeError(
                    f"verification failed: ownerId={verified.get('ownerId')} "
                    f"parentId={verified.get('parentId')}"
                )
            moved += 1
            print(f"verified: {verified.get('name')} ({inode_id})")
        except RuntimeError as exc:
            failures.append({"id": inode_id, "name": str(asset.get("name")), "error": str(exc)})
            print(f"FAILED: {asset.get('name')}: {exc}", file=sys.stderr)

    result = {"discovered": len(assets), "verifiedMoved": moved, "failures": failures}
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
