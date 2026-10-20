#!/usr/bin/env python3
"""Interactive Sigma workbook move: fzf folder browser, multi-select, owner choice."""

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

KEY_USE = "__use_this_folder__"
KEY_BACK = "__back__"
KEY_CANCEL = "__cancel__"
KEY_DEEP = "__deep_scan_workbooks__"
KEY_MODE_URL = "__mode_url__"
KEY_MODE_BROWSE = "__mode_browse__"

_WORKBOOK_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def die(message: str, code: int = 1) -> None:
    eprint(message)
    raise SystemExit(code)


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def load_env(path: Path) -> None:
    if not path.is_file():
        die(f"ERROR: missing Sigma env file: {path}")
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
    from sigma_license_audit import wait_with_braille

    proc = wait_with_braille(
        "Sigma POST /v2/auth/token",
        lambda: subprocess.run(
            ["bash", str(helper)], capture_output=True, text=True, check=False
        ),
    )
    if proc.returncode:
        sys.stderr.write(proc.stderr)
        die("ERROR: failed to mint Sigma API token")
    match = re.search(
        r"(?:export\s+)?SIGMA_API_TOKEN=(?:'([^']*)'|\"([^\"]*)\"|(\S+))",
        proc.stdout,
    )
    if not match:
        die("ERROR: token helper did not print SIGMA_API_TOKEN")
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
            if isinstance(response, list):
                return response
            rows.extend(response.get("entries") or response.get("workspaces") or [])
            page = response.get("nextPage")
            if not page:
                return rows


def member_display(row: dict[str, Any]) -> str:
    name = f"{row.get('firstName') or ''} {row.get('lastName') or ''}".strip()
    email = row.get("email") or ""
    if name:
        return f"{email} — {name}"
    return str(email)


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
        die(f"ERROR: member {query!r} matched {len(matches)} users")
    return matches[0]


def require_fzf() -> None:
    if not shutil_which("fzf"):
        die("ERROR: fzf not found (brew install fzf)")


def shutil_which(cmd: str) -> str | None:
    from shutil import which

    return which(cmd)


def fzf_pick(
    entries: list[tuple[str, str]],
    *,
    header: str,
    prompt: str,
    height: str = "70%",
    multi: bool = False,
) -> list[str] | str | None:
    if not entries:
        return None
    require_fzf()
    lines = [f"{key}\t{label}" for key, label in entries]
    cmd = [
        "fzf",
        "--ansi",
        "--no-mouse",
        f"--height={height}",
        "--reverse",
        "--border=rounded",
        "--cycle",
        "--delimiter=\t",
        "--with-nth=2",
        f"--prompt={prompt}",
        "--pointer=▶",
        f"--header={header}",
        "--footer=Enter=select · Esc=cancel",
    ]
    if multi:
        cmd.extend(["--multi", "--marker=◉ "])
    proc = subprocess.run(
        cmd,
        input="\n".join(lines) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    if multi:
        keys = []
        for line in proc.stdout.strip().split("\n"):
            keys.append(line.split("\t", 1)[0].strip())
        return keys
    return proc.stdout.split("\t", 1)[0].strip()


def direct_children(
    api: SigmaApi, folder_id: str, include_archived: bool
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "parentId": folder_id,
        "directChildFilter": "true",
        "limit": 1000,
    }
    if include_archived:
        params["includeArchived"] = "true"
    return api.paginate("/v2/files", params)


def folder_path(api: SigmaApi, folder_id: str) -> str:
    try:
        meta = api.request("GET", f"/v2/files/{folder_id}")
    except RuntimeError:
        return folder_id
    return str(meta.get("path") or meta.get("name") or folder_id)


def parse_workbook_inode_ref(text: str) -> str | None:
    """Extract workbook inode UUID from a Sigma URL, inode id, or bare UUID."""
    raw = text.strip()
    if not raw:
        return None
    if re.fullmatch(r"[0-9a-f-]{36}", raw, re.IGNORECASE):
        return raw
    match = re.search(r"/workbook/([0-9a-f-]{36})", raw, re.IGNORECASE)
    if match:
        return match.group(1)
    match = _WORKBOOK_UUID.search(raw)
    return match.group(0) if match else None


def load_workbook_by_inode(api: SigmaApi, inode_id: str) -> dict[str, Any]:
    meta = api.request("GET", f"/v2/files/{inode_id}")
    if meta.get("type") != "workbook":
        die(f"ERROR: {inode_id} is not a workbook (type={meta.get('type')})")
    meta["id"] = inode_id
    return meta


def prompt_workbooks_from_urls(api: SigmaApi) -> list[dict[str, Any]]:
    eprint("Paste Sigma workbook URL or inode id (empty line when done):")
    books: list[dict[str, Any]] = []
    seen: set[str] = set()
    while True:
        try:
            line = input("workbook URL> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            break
        inode = parse_workbook_inode_ref(line)
        if not inode:
            eprint(f"Could not parse workbook id from: {line}")
            continue
        if inode in seen:
            continue
        seen.add(inode)
        books.append(load_workbook_by_inode(api, inode))
        eprint(f"  + {books[-1].get('name')} ({inode})")
    return books


def pick_source_mode() -> str | None:
    choice = fzf_pick(
        [
            (KEY_MODE_URL, "Paste workbook URL(s) — then pick destination only"),
            (KEY_MODE_BROWSE, "Browse folders — pick source folder and workbooks"),
        ],
        header="How do you want to choose workbooks to move?",
        prompt="source > ",
    )
    return choice


def workspace_root_inode(workspace: dict[str, Any]) -> str:
    for key in ("inodeId", "folderId", "rootInodeId", "id", "workspaceId"):
        value = workspace.get(key)
        if value:
            return str(value)
    die(f"ERROR: cannot resolve folder inode for workspace {workspace.get('name')!r}")


def pick_start_folder(api: SigmaApi, members: list[dict[str, Any]]) -> str | None:
    rows: list[tuple[str, str]] = []
    for member in sorted(members, key=lambda m: str(m.get("email") or "")):
        home = member.get("homeFolderId")
        if not home:
            continue
        rows.append(
            (
                f"home:{home}",
                f"Member: {member_display(member)} (My Documents)",
            )
        )
    try:
        workspaces = api.paginate("/v2/workspaces", {"limit": 1000})
    except RuntimeError as exc:
        eprint(f"WARNING: could not list workspaces: {exc}")
        workspaces = []
    for ws in sorted(workspaces, key=lambda w: str(w.get("name") or "")):
        inode = workspace_root_inode(ws)
        rows.append((f"ws:{inode}", f"Workspace: {ws.get('name') or inode}"))
    rows.append(("jump:", "Jump to folder ID (paste inode)…"))
    choice = fzf_pick(
        rows,
        header="Start browsing from  ·  member My Documents or workspace",
        prompt="root > ",
    )
    if not choice:
        return None
    if choice.startswith(("home:", "ws:")):
        return choice.split(":", 1)[1]
    if choice == "jump:":
        if not sys.stdin.isatty():
            die("ERROR: jump requires a TTY")
        raw = input("Folder inode ID: ").strip()
        return raw or None
    return None


def browse_folder(
    api: SigmaApi,
    start_folder_id: str,
    *,
    title: str,
    include_archived: bool,
) -> str | None:
    """fzf up/down folder navigation; returns selected folder inode or None."""
    stack: list[str] = []
    current = start_folder_id
    while True:
        path = folder_path(api, current)
        children = direct_children(api, current, include_archived)
        rows: list[tuple[str, str]] = [
            (KEY_USE, "✓  Use this folder"),
            ]
        if stack:
            rows.append((KEY_BACK, "..  Back up"))
        for child in sorted(children, key=lambda c: str(c.get("name") or "").casefold()):
            if child.get("type") != "folder":
                continue
            cid = str(child.get("id") or "")
            if not cid:
                continue
            archived = " [archived]" if child.get("isArchived") else ""
            rows.append((f"folder:{cid}", f"📁  {child.get('name') or cid}{archived}"))
        choice = fzf_pick(
            rows,
            header=f"{title}\n{path}",
            prompt="folder > ",
            height="75%",
        )
        if not choice:
            return None
        if choice == KEY_USE:
            return current
        if choice == KEY_BACK:
            if not stack:
                return None
            current = stack.pop()
            continue
        if choice.startswith("folder:"):
            stack.append(current)
            current = choice.split(":", 1)[1]
            continue
    return None


def workbooks_in_folder(
    api: SigmaApi, folder_id: str, include_archived: bool
) -> list[dict[str, Any]]:
    children = direct_children(api, folder_id, include_archived)
    books = [c for c in children if c.get("type") == "workbook"]
    return sorted(books, key=lambda b: str(b.get("name") or "").casefold())


def deep_scan_workbooks(api: SigmaApi, folder_id: str, path_prefix: str) -> list[dict[str, Any]]:
    """Fallback when direct children omit workbooks (Sigma search listing caveat)."""
    prefix = path_prefix.rstrip("/")
    found: dict[str, dict[str, Any]] = {}
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
            wb_id = str(row.get("workbookId") or row.get("id") or "")
            if not wb_id:
                continue
            row_path = str(row.get("path") or "")
            parent = str(row.get("parentId") or "")
            if parent == folder_id or (
                prefix and row_path.startswith(prefix + "/")
            ):
                row = dict(row)
                row["id"] = wb_id
                row.setdefault("type", "workbook")
                found[wb_id] = row
    return sorted(found.values(), key=lambda b: str(b.get("name") or "").casefold())


def pick_workbooks(
    api: SigmaApi,
    folder_id: str,
    include_archived: bool,
) -> list[dict[str, Any]] | None:
    path = folder_path(api, folder_id)
    books = workbooks_in_folder(api, folder_id, include_archived)
    while True:
        rows: list[tuple[str, str]] = []
        if not books:
            rows.append(
                (KEY_DEEP, "Search workbooks API for this folder path (listing fallback)")
            )
        for book in books:
            bid = str(book.get("id") or "")
            arch = " [archived]" if book.get("isArchived") else ""
            rows.append((bid, f"{book.get('name') or bid}{arch}"))
        if not rows:
            eprint(f"No workbooks in folder (direct children). Path: {path}")
            return []
        header = f"Select workbooks (TAB multi) · Esc back to folder browser\n{path}"
        if len(rows) == 1 and rows[0][0] == KEY_DEEP:
            choice = fzf_pick(rows, header=header, prompt="workbook > ")
            if not choice:
                return None
            if choice == KEY_DEEP:
                books = deep_scan_workbooks(api, folder_id, path)
                continue
            return []
        selected = fzf_pick(
            rows,
            header=header,
            prompt="workbook > ",
            multi=True,
            height="80%",
        )
        if selected is None:
            return None
        if not selected:
            continue
        if KEY_DEEP in selected:
            books = deep_scan_workbooks(api, folder_id, path)
            continue
        by_id = {str(b.get("id")): b for b in books}
        out: list[dict[str, Any]] = []
        for sid in selected:
            if sid in by_id:
                out.append(by_id[sid])
                continue
            try:
                meta = api.request("GET", f"/v2/files/{sid}")
                if meta.get("type") == "workbook":
                    meta["id"] = sid
                    out.append(meta)
            except RuntimeError as exc:
                eprint(f"WARNING: skip {sid}: {exc}")
        return out


def pick_owner(members: list[dict[str, Any]]) -> tuple[str, str | None]:
    """Returns ('keep', None) or ('assign', memberId)."""
    rows = [
        ("keep", "Keep current owner for each workbook"),
        ("assign", "Assign a new owner (pick member next)"),
    ]
    mode = fzf_pick(rows, header="Owner after move", prompt="owner > ")
    if not mode:
        return "cancel", None
    if mode == "keep":
        return "keep", None
    member_rows = [
        (str(m["memberId"]), member_display(m))
        for m in sorted(members, key=lambda x: str(x.get("email") or ""))
    ]
    mid = fzf_pick(
        member_rows,
        header="New owner",
        prompt="member > ",
        height="80%",
    )
    if not mid:
        return "cancel", None
    return "assign", mid


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


def patch_move(
    api: SigmaApi,
    inode_id: str,
    dest_parent_id: str,
    new_owner_id: str | None,
) -> dict[str, Any]:
    body: dict[str, str] = {"parentId": dest_parent_id}
    if new_owner_id:
        body["ownerId"] = new_owner_id
    try:
        api.request("PATCH", f"/v2/files/{inode_id}", body=body)
    except RuntimeError as exc:
        if "inode_archived" not in str(exc):
            raise
        restore_inode_and_archived_ancestors(api, inode_id)
        api.request("PATCH", f"/v2/files/{inode_id}", body=body)
    return api.request("GET", f"/v2/files/{inode_id}")


def should_apply(args: argparse.Namespace) -> bool:
    if not sys.stdin.isatty():
        if args.execute and args.yes:
            return True
        die("ERROR: non-interactive apply requires --execute --yes")
    if args.yes:
        return True
    eprint("Apply move? Press Enter to confirm, or N to cancel.")
    try:
        answer = input("> ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer not in {"n", "no"}


def interactive_flow(api: SigmaApi, args: argparse.Namespace) -> int:
    members = api.paginate(
        "/v2/members",
        {"limit": 1000, "includeArchived": "true", "includeInactive": "true"},
    )

    workbooks: list[dict[str, Any]] = []
    source_folder = args.source_folder_id

    for url in args.workbook_urls:
        inode = parse_workbook_inode_ref(url)
        if not inode:
            die(f"ERROR: could not parse workbook id from: {url}")
        workbooks.append(load_workbook_by_inode(api, inode))

    if args.inode_ids:
        for iid in args.inode_ids:
            workbooks.append(load_workbook_by_inode(api, iid))
    if workbooks and not source_folder:
        source_folder = str(workbooks[0].get("parentId") or "")

    if workbooks:
        pass
    elif not source_folder:
        mode = pick_source_mode()
        if not mode:
            return 0
        if mode == KEY_MODE_URL:
            workbooks = prompt_workbooks_from_urls(api)
            if not workbooks:
                return 0
            source_folder = str(workbooks[0].get("parentId") or "")
        elif mode == KEY_MODE_BROWSE:
            while True:
                start = pick_start_folder(api, members)
                if not start:
                    return 0
                source_folder = browse_folder(
                    api,
                    start,
                    title="SOURCE — pick folder containing workbooks",
                    include_archived=args.include_archived,
                )
                if not source_folder:
                    return 0
                picked = pick_workbooks(api, source_folder, args.include_archived)
                if picked is None:
                    continue
                workbooks = picked
                break
        else:
            return 0
    elif source_folder:
        picked = pick_workbooks(api, source_folder, args.include_archived)
        if picked is None:
            return 0
        workbooks = picked

    if not workbooks:
        eprint("No workbooks selected.")
        return 0

    dest_folder = args.destination_folder_id
    if not dest_folder:
        dest_start = pick_start_folder(api, members)
        if not dest_start:
            return 0
        dest_folder = browse_folder(
            api,
            dest_start,
            title="DESTINATION — pick target folder",
            include_archived=args.include_archived,
        )
        if not dest_folder:
            return 0

    if args.owner == "keep":
        owner_mode, new_owner_id = "keep", None
    elif args.owner:
        owner_mode, new_owner_id = "assign", resolve_member(members, args.owner)["memberId"]
    else:
        owner_mode, new_owner_id = pick_owner(members)
        if owner_mode == "cancel":
            return 0

    dest_path = folder_path(api, dest_folder)
    source_path = folder_path(api, source_folder)
    plan = {
        "sourceFolderId": source_folder,
        "sourcePath": source_path,
        "destinationFolderId": dest_folder,
        "destinationPath": dest_path,
        "owner": owner_mode,
        "newOwnerId": new_owner_id,
        "workbooks": [
            {
                "id": w.get("id"),
                "name": w.get("name"),
                "ownerId": w.get("ownerId"),
                "path": w.get("path"),
            }
            for w in workbooks
        ],
    }
    eprint("\nMove plan:")
    eprint(f"  From: {source_path}")
    eprint(f"  To:   {dest_path}")
    eprint(f"  Owner: {'unchanged' if owner_mode == 'keep' else new_owner_id}")
    eprint(f"  Count: {len(workbooks)} workbook(s)")
    print(json.dumps(plan, indent=2, sort_keys=True))

    if not should_apply(args):
        eprint("Aborted — no changes made.")
        return 0

    failures: list[dict[str, str]] = []
    moved = 0
    for book in workbooks:
        inode_id = str(book.get("id") or "")
        prior_owner = str(book.get("ownerId") or "")
        expected_owner = new_owner_id if owner_mode == "assign" else prior_owner
        try:
            verified = patch_move(api, inode_id, dest_folder, new_owner_id)
            parent_ok = str(verified.get("parentId") or "") == dest_folder
            owner_ok = str(verified.get("ownerId") or "") == str(expected_owner)
            if not parent_ok or not owner_ok:
                raise RuntimeError(
                    f"verify failed parentId={verified.get('parentId')} "
                    f"ownerId={verified.get('ownerId')}"
                )
            moved += 1
            eprint(f"OK: {verified.get('name')} ({inode_id})")
        except RuntimeError as exc:
            failures.append({"id": inode_id, "name": str(book.get("name")), "error": str(exc)})
            eprint(f"FAILED: {book.get('name')}: {exc}", file=sys.stderr)

    summary = {"moved": moved, "failed": len(failures), "failures": failures}
    print(json.dumps(summary, indent=2))
    return 1 if failures else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-folder-id", help="Skip source browser")
    parser.add_argument("--destination-folder-id", help="Skip destination browser")
    parser.add_argument(
        "--workbook-url",
        action="append",
        dest="workbook_urls",
        default=[],
        help="Sigma workbook URL or inode id (skip source browser)",
    )
    parser.add_argument(
        "--inode-id",
        action="append",
        dest="inode_ids",
        default=[],
        help="Workbook inode(s); skip workbook picker",
    )
    parser.add_argument(
        "--inode-ids-file",
        type=Path,
        help="File with one workbook inode per line",
    )
    parser.add_argument(
        "--owner",
        help="keep, or member email/id/name for new owner",
    )
    parser.add_argument("--include-archived", action="store_true")
    parser.add_argument("--execute", action="store_true", help="Allow apply after confirm")
    parser.add_argument("--yes", action="store_true", help="Skip confirm with --execute")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--token-helper", type=Path, default=DEFAULT_TOKEN_HELPER)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.inode_ids_file:
        text = args.inode_ids_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                args.inode_ids.append(line)

    has_workbooks = bool(args.inode_ids or args.workbook_urls)
    if not sys.stdin.isatty() and not (
        args.destination_folder_id and has_workbooks
    ):
        die(
            "ERROR: non-interactive use requires --destination-folder-id and "
            "--workbook-url or --inode-id"
        )

    load_env(args.env)
    token = mint_token(args.token_helper)
    base_url = os.environ.get("SIGMA_BASE_URL", "")
    if not base_url:
        die("ERROR: SIGMA_BASE_URL is not configured")
    api = SigmaApi(base_url, token)
    return interactive_flow(api, args)


if __name__ == "__main__":
    raise SystemExit(main())
