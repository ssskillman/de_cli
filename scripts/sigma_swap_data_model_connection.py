#!/usr/bin/env python3
"""Swap a Sigma data-model connection, or transfer one data model or dataset."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
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
    member_display,
    mint_token,
    resolve_member,
)

FZF_WRAP_UP = '--bind=up:transform:[ "$FZF_POS" -eq 1 ] && echo last || echo up'
FZF_WRAP_DOWN = (
    '--bind=down:transform:[ "$FZF_POS" -ge "$FZF_MATCH_COUNT" ] '
    "&& echo first || echo down"
)


def fzf_command(*, header: str, prompt: str) -> list[str]:
    """Type-to-filter fzf. Up/Down wrap at the ends of the filtered list."""
    return [
        "fzf",
        "--ansi",
        "--no-mouse",
        "--height=70%",
        "--reverse",
        "--border=rounded",
        "--cycle",
        "--delimiter=\t",
        "--with-nth=2",
        f"--prompt={prompt}",
        "--pointer=▶",
        f"--header={header}",
        "--info=inline",
        "--footer=Type to filter · Up/Down wraps · Enter selects · Esc cancels",
        FZF_WRAP_UP,
        FZF_WRAP_DOWN,
    ]


def fzf_pick(
    entries: list[tuple[str, str]], *, header: str, prompt: str
) -> str | None:
    if not entries:
        return None
    proc = subprocess.run(
        fzf_command(header=header, prompt=prompt),
        input="\n".join(f"{key}\t{label}" for key, label in entries) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.split("\t", 1)[0].strip()


def unique_match(
    rows: list[dict[str, Any]], query: str, *id_fields: str, name_field: str = "name"
) -> dict[str, Any]:
    """Match one row by id, else by exact name. Duplicate names must use an id."""
    needle = query.strip().casefold()
    if not needle:
        raise ValueError("empty selection")
    by_id = [
        row
        for row in rows
        if any(str(row.get(field) or "").casefold() == needle for field in id_fields)
    ]
    if len(by_id) == 1:
        return by_id[0]
    by_name = [
        row for row in rows if str(row.get(name_field) or "").casefold() == needle
    ]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise ValueError(
            f"{query!r} matches {len(by_name)} names; pass the id to choose one"
        )
    raise ValueError(f"{query!r} matched nothing")


def swap_body_whole(from_id: str, to_id: str) -> dict[str, Any]:
    return {
        "connectionMapping": [
            {"fromId": from_id, "toId": to_id, "swapTables": True}
        ]
    }


def swap_body_table(from_id: str, to_id: str, path: list[str]) -> dict[str, Any]:
    if not path:
        raise ValueError("table source has no warehouse path")
    return {
        "connectionMapping": [
            {
                "fromId": from_id,
                "toId": to_id,
                "swapTables": True,
                "paths": [{"fromPath": list(path), "toPath": list(path)}],
            }
        ]
    }


def swap_body_custom_sql(from_id: str, to_id: str) -> dict[str, Any]:
    return {
        "connectionMapping": [
            {"fromId": from_id, "toId": to_id, "swapTables": False}
        ]
    }


def custom_sql_on_connection(
    sources: list[dict[str, Any]], connection_id: str
) -> list[dict[str, Any]]:
    return [
        source
        for source in sources
        if source.get("type") == "custom-sql"
        and str(source.get("connectionId") or "") == connection_id
    ]


def build_swap_request(
    *,
    scope: str,
    source: dict[str, Any] | None,
    sources: list[dict[str, Any]],
    from_id: str,
    to_id: str,
    table_path: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Return the swapSources body and warnings. Raises for an unsafe source."""
    if not from_id or not to_id:
        raise ValueError("from and to connections are required")
    if from_id == to_id:
        raise ValueError("from and to connections are the same")
    if scope == "whole":
        return swap_body_whole(from_id, to_id), []
    if source is None:
        raise ValueError("one-source swap needs a source")
    kind = str(source.get("type") or "")
    if kind == "table":
        return swap_body_table(from_id, to_id, list(table_path or [])), []
    if kind == "custom-sql":
        warnings: list[str] = []
        shared = custom_sql_on_connection(sources, from_id)
        if len(shared) > 1:
            warnings.append(
                f"{len(shared)} custom SQL sources use this connection; "
                "Sigma changes all of them."
            )
        return swap_body_custom_sql(from_id, to_id), warnings
    if kind in {"dataset", "data-model"}:
        raise ValueError(
            "This source is a dataset or data model, not a warehouse path. "
            "A connection swap cannot retarget it; it needs a replacement source id."
        )
    raise ValueError(f"unsupported source type {kind!r}")


def ownership_body(member_id: str) -> dict[str, str]:
    return {"ownerId": member_id}


def validate_new_owner(member: dict[str, Any], current_owner_id: str) -> str:
    if member.get("isArchived") or member.get("isInactive"):
        raise ValueError("new owner must be an active Sigma member")
    new_id = str(member.get("memberId") or "")
    if not new_id:
        raise ValueError("selected member has no memberId")
    if new_id == current_owner_id:
        raise ValueError("selected member already owns this asset")
    return new_id


def source_key(source: dict[str, Any], index: int) -> str:
    kind = str(source.get("type") or "source")
    for field in ("tableId", "customSqlId", "datasetId", "dataModelId"):
        if source.get(field):
            return f"{kind}:{source[field]}"
    return f"{kind}:{index}"


def resolve_source_choice(
    labeled: list[tuple[str, str, dict[str, Any], list[str] | None]],
    query: str,
) -> tuple[dict[str, Any], list[str] | None]:
    """Match a source key, inode id, or exact label. Duplicate labels need an id."""
    needle = query.strip().casefold()
    if not needle:
        raise ValueError("empty source selection")
    by_key = [item for item in labeled if item[0].casefold() == needle]
    if len(by_key) == 1:
        return by_key[0][2], by_key[0][3]
    matches: list[tuple[dict[str, Any], list[str] | None]] = []
    for _key, label, row, path in labeled:
        ids = {
            str(row.get(field) or "").casefold()
            for field in ("tableId", "customSqlId", "datasetId", "dataModelId")
            if row.get(field)
        }
        if needle in ids or label.casefold() == needle:
            matches.append((row, path))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"{query!r} matches {len(matches)} sources; pass the source id"
        )
    raise ValueError(f"{query!r} matched no source")


def source_label(source: dict[str, Any], path: list[str] | None = None) -> str:
    kind = str(source.get("type") or "source")
    if kind == "table":
        shown = ".".join(path) if path else str(source.get("tableId") or "")
        return f"table  {shown}"
    if kind == "custom-sql":
        return f"custom-sql  {source.get('customSqlId') or ''}"
    if kind == "dataset":
        return f"dataset  {source.get('datasetId') or ''}"
    if kind == "data-model":
        elements = ",".join(source.get("elementIds") or [])
        return f"data-model  {source.get('dataModelId') or ''}  {elements}".rstrip()
    return kind


def asset_label(asset: dict[str, Any]) -> str:
    owner = asset.get("ownerLabel") or asset.get("ownerId") or ""
    path = asset.get("path") or ""
    return f"{asset.get('kind')}  {asset.get('name')}  {path}  {owner}".rstrip()


def connection_label(row: dict[str, Any]) -> str:
    return f"{row.get('name')}  {row.get('type') or ''}  {row.get('connectionId')}"


def paginate_optional_admin(
    api: SigmaApi, path: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    try:
        return api.paginate(path, {**params, "skipPermissionCheck": "true"})
    except RuntimeError as exc:
        if "(403)" not in str(exc):
            raise
        eprint("skipPermissionCheck denied; listing what this token can see.")
        return api.paginate(path, params)


def list_data_models(api: SigmaApi) -> list[dict[str, Any]]:
    return paginate_optional_admin(api, "/v2/dataModels", {"limit": 1000})


def list_datasets(api: SigmaApi) -> list[dict[str, Any]]:
    return paginate_optional_admin(api, "/v2/datasets", {"limit": 1000})


def list_connections(api: SigmaApi) -> list[dict[str, Any]]:
    rows = api.paginate(
        "/v2/connections", {"limit": 1000, "includeArchived": "false"}
    )
    return [row for row in rows if not row.get("isArchived")]


def list_sources(api: SigmaApi, data_model_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        data = api.request(
            "GET", f"/v2/dataModels/{data_model_id}/sources", params
        )
        rows.extend(data.get("entries") or [])
        token = data.get("nextPageToken") or None
        if not token:
            return rows


def table_connection_path(api: SigmaApi, table_id: str) -> tuple[str, list[str]]:
    meta = api.request("GET", f"/v2/connections/paths/{table_id}")
    path = list(meta.get("path") or [])
    connection_id = str(meta.get("connectionId") or "")
    if not connection_id or not path:
        raise ValueError(f"table {table_id} has no warehouse connection path")
    return connection_id, path


def model_assets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": "data-model",
            "id": row.get("dataModelId"),
            "name": row.get("name") or "",
            "path": row.get("path") or "",
            "ownerId": str(row.get("ownerId") or ""),
        }
        for row in rows
        if row.get("dataModelId")
    ]


def dataset_assets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "kind": "dataset",
            "id": row.get("datasetId"),
            "name": row.get("name") or "",
            "path": row.get("path") or "",
            "ownerId": str(row.get("ownerId") or row.get("owner") or ""),
        }
        for row in rows
        if row.get("datasetId")
    ]


def apply_swap(api: SigmaApi, data_model_id: str, body: dict[str, Any]) -> Any:
    return api.request(
        "POST", f"/v2/dataModels/{data_model_id}/swapSources", body=body
    )


def apply_owner(api: SigmaApi, inode_id: str, member_id: str) -> dict[str, Any]:
    api.request("PATCH", f"/v2/files/{inode_id}", body=ownership_body(member_id))
    verified = api.request("GET", f"/v2/files/{inode_id}")
    if str(verified.get("ownerId") or "") != member_id:
        raise RuntimeError(
            f"ownership verification failed: ownerId={verified.get('ownerId')}"
        )
    return verified


def confirm(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().casefold()
    except (EOFError, KeyboardInterrupt):
        eprint("\nCancelled.")
        return False
    return answer in {"y", "yes"}


def _require_one(found: str | None, what: str) -> str:
    if not found:
        raise SystemExit(f"Cancelled {what}.")
    return found


def _pick_connection(
    connections: list[dict[str, Any]], *, header: str, prompt: str
) -> dict[str, Any]:
    chosen = _require_one(
        fzf_pick(
            [(str(row["connectionId"]), connection_label(row)) for row in connections],
            header=header,
            prompt=prompt,
        ),
        "connection pick",
    )
    return unique_match(connections, chosen, "connectionId")


def run_swap(api: SigmaApi, args: argparse.Namespace) -> int:
    eprint("Loading data models…")
    models = list_data_models(api)
    if args.data_model:
        model = unique_match(models, args.data_model, "dataModelId")
    elif sys.stdin.isatty():
        chosen = _require_one(
            fzf_pick(
                [
                    (
                        str(row["dataModelId"]),
                        f"{row.get('name')}  {row.get('path') or ''}  {row.get('dataModelId')}",
                    )
                    for row in models
                ],
                header="Data model",
                prompt="model> ",
            ),
            "data model pick",
        )
        model = unique_match(models, chosen, "dataModelId")
    else:
        raise SystemExit("ERROR: pass --data-model")

    data_model_id = str(model["dataModelId"])
    eprint(f"Loading sources for {model.get('name')}…")
    sources = list_sources(api, data_model_id)
    scope = args.scope
    source: dict[str, Any] | None = None
    table_path: list[str] | None = None
    inferred_from: str | None = None

    if args.source:
        scope = "source"
    if scope is None and sys.stdin.isatty():
        scope = _require_one(
            fzf_pick(
                [
                    ("whole", "Whole data model"),
                    ("source", "One source"),
                ],
                header="Swap scope",
                prompt="scope> ",
            ),
            "scope pick",
        )
    scope = scope or "whole"
    if scope not in {"whole", "source"}:
        raise SystemExit("ERROR: --scope must be whole or source")

    if scope == "source":
        labeled: list[tuple[str, str, dict[str, Any], list[str] | None]] = []
        for index, row in enumerate(sources):
            path = None
            if row.get("type") == "table" and row.get("tableId"):
                try:
                    _conn, path = table_connection_path(api, str(row["tableId"]))
                    inferred = _conn
                    row = {**row, "connectionId": row.get("connectionId") or inferred}
                except (RuntimeError, ValueError) as exc:
                    eprint(f"WARNING: {exc}")
            labeled.append((source_key(row, index), source_label(row, path), row, path))
        if args.source:
            source, table_path = resolve_source_choice(labeled, args.source)
        elif sys.stdin.isatty():
            chosen = _require_one(
                fzf_pick(
                    [(key, label) for key, label, _row, _path in labeled],
                    header="Source",
                    prompt="source> ",
                ),
                "source pick",
            )
            for key, _label, row, path in labeled:
                if key == chosen:
                    source = row
                    table_path = path
                    break
        else:
            raise SystemExit("ERROR: pass --source")
        if source is None:
            raise SystemExit("ERROR: source not found")
        inferred_from = str(source.get("connectionId") or "") or None
        if source.get("type") == "table" and source.get("tableId") and table_path is None:
            inferred_from, table_path = table_connection_path(api, str(source["tableId"]))

    eprint("Loading connections…")
    connections = list_connections(api)
    if args.from_connection:
        from_row = unique_match(connections, args.from_connection, "connectionId")
    elif inferred_from:
        from_row = unique_match(connections, inferred_from, "connectionId")
        eprint(f"Current connection: {from_row.get('name')}")
    elif sys.stdin.isatty():
        from_row = _pick_connection(
            connections, header="Current connection", prompt="from> "
        )
    else:
        raise SystemExit("ERROR: pass --from-connection")

    if args.to_connection:
        to_row = unique_match(connections, args.to_connection, "connectionId")
    elif sys.stdin.isatty():
        to_row = _pick_connection(
            connections, header="New connection", prompt="to> "
        )
    else:
        raise SystemExit("ERROR: pass --to-connection")

    body, warnings = build_swap_request(
        scope=scope,
        source=source,
        sources=sources,
        from_id=str(from_row["connectionId"]),
        to_id=str(to_row["connectionId"]),
        table_path=table_path,
    )
    plan = {
        "action": "swap",
        "dataModelId": data_model_id,
        "name": model.get("name"),
        "scope": scope,
        "request": body,
        "warnings": warnings,
    }
    print(json.dumps(plan, indent=2))
    for warning in warnings:
        eprint(f"WARNING: {warning}")
    return _maybe_apply(
        args,
        lambda: apply_swap(api, data_model_id, body),
        "Apply this connection swap? [y/N] ",
    )


def run_owner(api: SigmaApi, args: argparse.Namespace) -> int:
    if args.dataset and args.data_model:
        raise SystemExit("ERROR: pass only one of --dataset or --data-model")
    eprint("Loading data models and datasets…")
    assets = model_assets(list_data_models(api)) + dataset_assets(list_datasets(api))
    if args.dataset:
        asset = unique_match(
            [row for row in assets if row["kind"] == "dataset"],
            args.dataset,
            "id",
        )
    elif args.data_model:
        asset = unique_match(
            [row for row in assets if row["kind"] == "data-model"],
            args.data_model,
            "id",
        )
    elif sys.stdin.isatty():
        chosen = _require_one(
            fzf_pick(
                [(str(row["id"]), asset_label(row)) for row in assets],
                header="Data model or dataset",
                prompt="asset> ",
            ),
            "asset pick",
        )
        asset = unique_match(assets, chosen, "id")
    else:
        raise SystemExit("ERROR: pass --data-model or --dataset")

    inode_id = str(asset["id"])
    current = api.request("GET", f"/v2/files/{inode_id}")
    current_owner = str(current.get("ownerId") or "")
    eprint("Loading members…")
    members = api.paginate(
        "/v2/members",
        {"limit": 1000, "includeArchived": "true", "includeInactive": "true"},
    )
    active = [
        row
        for row in members
        if not row.get("isArchived") and not row.get("isInactive")
    ]
    if args.new_owner:
        member = resolve_member(members, args.new_owner)
    elif sys.stdin.isatty():
        chosen = _require_one(
            fzf_pick(
                [(str(row["memberId"]), member_display(row)) for row in active],
                header="New owner",
                prompt="owner> ",
            ),
            "owner pick",
        )
        member = next(row for row in active if str(row["memberId"]) == chosen)
    else:
        raise SystemExit("ERROR: pass --new-owner")

    new_id = validate_new_owner(member, current_owner)
    plan = {
        "action": "owner",
        "kind": asset["kind"],
        "id": inode_id,
        "name": asset.get("name"),
        "path": asset.get("path"),
        "currentOwnerId": current_owner,
        "newOwner": member_display(member),
        "request": ownership_body(new_id),
    }
    print(json.dumps(plan, indent=2))
    return _maybe_apply(
        args,
        lambda: apply_owner(api, inode_id, new_id),
        "Transfer ownership? Folder stays put. [y/N] ",
    )


def _maybe_apply(args: argparse.Namespace, apply, prompt: str) -> int:
    if args.yes and not args.execute:
        raise SystemExit("ERROR: --yes requires --execute")
    should_apply = bool(args.execute and args.yes)
    if sys.stdin.isatty() and not should_apply:
        should_apply = confirm(prompt)
    if not should_apply:
        eprint("Preview only. Nothing changed.")
        return 0
    result = apply()
    print(json.dumps(result, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Swap a data model connection, or transfer one data model or dataset."
        )
    )
    parser.add_argument("--action", choices=("swap", "owner"))
    parser.add_argument("--data-model")
    parser.add_argument("--dataset")
    parser.add_argument("--scope", choices=("whole", "source"))
    parser.add_argument("--source")
    parser.add_argument("--from-connection")
    parser.add_argument("--to-connection")
    parser.add_argument("--new-owner")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--token-helper", type=Path, default=DEFAULT_TOKEN_HELPER)
    args = parser.parse_args(argv)

    action = args.action
    if action is None and sys.stdin.isatty():
        action = fzf_pick(
            [
                ("swap", "Swap a data model connection"),
                ("owner", "Transfer ownership of one data model or dataset"),
            ],
            header="Action",
            prompt="action> ",
        )
        if not action:
            eprint("Cancelled.")
            return 0
    if action not in {"swap", "owner"}:
        raise SystemExit("ERROR: pass --action swap or --action owner")

    load_env(args.env)
    base_url = os.environ.get("SIGMA_BASE_URL", "").rstrip("/")
    if not base_url:
        raise SystemExit("ERROR: SIGMA_BASE_URL is empty")
    token = os.environ.get("SIGMA_API_TOKEN") or mint_token(args.token_helper)
    api = SigmaApi(base_url, token)
    if action == "swap":
        return run_swap(api, args)
    return run_owner(api, args)


if __name__ == "__main__":
    raise SystemExit(main())
