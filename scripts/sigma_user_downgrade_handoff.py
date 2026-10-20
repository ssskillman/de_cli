#!/usr/bin/env python3
"""Downgrade a Sigma member and migrate their My Documents contents."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_SIGMA_ENV = Path.home() / ".config" / "sigma" / "env"
DEFAULT_GET_TOKEN = (
    Path.home() / "github" / "sigma-skills" / "sigma-api" / "scripts" / "get-token.sh"
)
DEFAULT_BOOTSTRAP = Path.home() / "de" / "sigma_cli_bootstrap.py"


@dataclass(frozen=True)
class Member:
    member_id: str
    email: str
    member_type: str
    home_folder_id: str
    first_name: str = ""
    last_name: str = ""
    is_archived: bool = False
    is_inactive: bool = False

    @property
    def label(self) -> str:
        name = " ".join(p for p in (self.first_name, self.last_name) if p).strip()
        return f"{self.email} ({name}, {self.member_type}, {self.member_id})"


class SigmaError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Downgrade a Sigma user's account type and migrate the top-level "
            "contents of their My Documents folder to another Sigma user's My Documents."
        )
    )
    parser.add_argument(
        "--user",
        "--source",
        dest="sources",
        action="append",
        help="Source user email, member ID, or unique name match. Repeat for multiple users.",
    )
    parser.add_argument(
        "--to",
        "--new-owner",
        dest="new_owner",
        help="Destination Sigma user email, member ID, or unique name match.",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Walk through the choices in a menu. This is automatic when no users are supplied.",
    )
    parser.add_argument(
        "--member-type",
        "--account-type",
        default=os.environ.get("SIGMA_DOWNGRADE_MEMBER_TYPE", "View"),
        help="Target Sigma memberType/account type after migration. Default: View.",
    )
    parser.add_argument(
        "--destination-folder-id",
        help=(
            "Existing folder ID to receive migrated contents. If omitted, --execute "
            "creates one folder per source under the target user's My Documents."
        ),
    )
    parser.add_argument(
        "--destination-folder-name",
        help=(
            "Folder name to create under the target user's My Documents. With multiple "
            "sources, the source email is appended unless --destination-folder-id is used."
        ),
    )
    parser.add_argument(
        "--include-archived-files",
        action="store_true",
        help="Include archived files when listing the source My Documents folder.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the migration and memberType change. Default is dry-run.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation required with --execute.",
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
        help="Path to the Sigma CLI binary. Default: resolve from PATH or ~/.sigma-cli/bin.",
    )
    parser.add_argument(
        "--sigma-env",
        type=Path,
        default=Path(os.environ.get("SIGMA_ENV", DEFAULT_SIGMA_ENV)),
        help=f"Sigma env file. Default: {DEFAULT_SIGMA_ENV}.",
    )
    parser.add_argument(
        "--get-token",
        type=Path,
        default=Path(os.environ.get("GET_TOKEN_SH", DEFAULT_GET_TOKEN)),
        help=f"Token helper script. Default: {DEFAULT_GET_TOKEN}.",
    )
    parser.add_argument(
        "--bootstrap",
        type=Path,
        default=Path(os.environ.get("BOOTSTRAP_PY", DEFAULT_BOOTSTRAP)),
        help=f"Sigma CLI profile bootstrap. Default: {DEFAULT_BOOTSTRAP}.",
    )
    return parser.parse_args()


def prompt_text(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\naborted")
        if value:
            return value
        if default is not None:
            return default
        print("Please enter a value.")


def prompt_choice(prompt: str, choices: list[str], default: int = 1) -> int:
    print(f"\n{prompt}")
    for index, choice in enumerate(choices, start=1):
        marker = " (default)" if index == default else ""
        print(f"  {index}. {choice}{marker}")
    while True:
        value = prompt_text("Choose", str(default))
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return int(value)
        print(f"Enter a number from 1 to {len(choices)}.")


def prompt_yes_no(prompt: str, default: bool = False) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        try:
            value = input(f"{prompt} [{default_text}]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\naborted")
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Enter y or n.")


def choose_member(
    prompt: str,
    members: list[Member],
    excluded_ids: set[str] | None = None,
) -> Member:
    available = [
        member
        for member in members
        if not member.is_archived
        and not member.is_inactive
        and member.member_id not in (excluded_ids or set())
    ]
    while True:
        query = prompt_text(f"\n{prompt} (name or email)").lower()
        matches = [
            member
            for member in available
            if query in member.email.lower()
            or query in member.member_id.lower()
            or query in f"{member.first_name} {member.last_name}".strip().lower()
        ]
        if not matches:
            print("No active Sigma users matched. Try another search.")
            continue
        matches.sort(key=lambda member: (member.email.lower(), member.member_id))
        if len(matches) > 20:
            print(f"{len(matches)} users matched. Narrow the search to see 20 or fewer.")
            continue
        choice = prompt_choice(
            f"Select a user ({len(matches)} match{'es' if len(matches) != 1 else ''})",
            [member.label for member in matches],
        )
        return matches[choice - 1]


def interactive_setup(
    args: argparse.Namespace,
    members: list[Member],
    account_types: set[str],
) -> None:
    print("\nSigma user downgrade and document handoff")
    print("Select the users and migration settings. Nothing changes until you confirm.")

    sources: list[Member] = []
    while True:
        source = choose_member(
            "Find the user to downgrade",
            members,
            excluded_ids={member.member_id for member in sources},
        )
        sources.append(source)
        if not prompt_yes_no("Add another source user?"):
            break

    target = choose_member(
        "Find the user who should receive the documents",
        members,
        excluded_ids={member.member_id for member in sources},
    )

    if account_types:
        type_choices = sorted(account_types, key=str.lower)
        default_type = next(
            (index for index, name in enumerate(type_choices, 1) if name == args.member_type),
            1,
        )
        selected = prompt_choice("Select the new license/account type", type_choices, default_type)
        args.member_type = type_choices[selected - 1]
    else:
        args.member_type = prompt_text("New license/account type", args.member_type)

    folder_choice = prompt_choice(
        "Where should the documents go?",
        [
            "Create a folder in the destination user's My Documents",
            "Use an existing destination folder ID",
        ],
    )
    if folder_choice == 1:
        custom_name = prompt_text(
            "Folder name (leave blank for an automatic name)",
            "",
        )
        args.destination_folder_name = custom_name or None
        args.destination_folder_id = None
    else:
        args.destination_folder_id = prompt_text("Existing destination folder ID")
        args.destination_folder_name = None

    args.include_archived_files = prompt_yes_no("Include archived files?")
    args.execute = (
        prompt_choice(
            "What should this run do?",
            ["Preview the plan only", "Apply the migration after final confirmation"],
        )
        == 2
    )
    args.sources = [source.email for source in sources]
    args.new_owner = target.email


def equivalent_command(args: argparse.Namespace) -> str:
    command = [str(Path(__file__).resolve())]
    for source in args.sources:
        command.extend(["--user", source])
    command.extend(["--to", args.new_owner, "--member-type", args.member_type])
    if args.destination_folder_id:
        command.extend(["--destination-folder-id", args.destination_folder_id])
    if args.destination_folder_name:
        command.extend(["--destination-folder-name", args.destination_folder_name])
    if args.include_archived_files:
        command.append("--include-archived-files")
    if args.execute:
        command.append("--execute")
    return shlex.join(command)


def die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def resolve_sigma_bin(value: str | None) -> str:
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        die(f"SIGMA_BIN is not executable: {candidate}")
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "sigma"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    candidate = Path.home() / ".sigma-cli" / "bin" / "sigma"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    die("Sigma CLI not found. Install it or pass --sigma-bin.")


def load_env_file(path: Path) -> None:
    if not path.is_file():
        die(f"missing Sigma env file: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ")
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        try:
            parsed = shlex.split(value.strip(), posix=True)
            os.environ.setdefault(key, parsed[0] if parsed else "")
        except ValueError:
            os.environ.setdefault(key, value.strip().strip("'\""))


def refresh_token(get_token: Path) -> None:
    if not get_token.is_file():
        die(f"missing Sigma token script: {get_token}")
    from sigma_license_audit import wait_with_braille

    proc = wait_with_braille(
        "Sigma POST /v2/auth/token",
        lambda: subprocess.run(
            ["bash", str(get_token)],
            check=False,
            text=True,
            capture_output=True,
        ),
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        die("failed to mint Sigma API token")
    match = re.search(
        r"(?:export\s+)?SIGMA_API_TOKEN=(?:'([^']*)'|\"([^\"]*)\"|(\S+))",
        proc.stdout,
    )
    if not match:
        die("token helper did not print SIGMA_API_TOKEN")
    os.environ["SIGMA_API_TOKEN"] = next(g for g in match.groups() if g is not None)
    print("Sigma token minted (expires in about 1 hour).", file=sys.stderr)


class Sigma:
    def __init__(self, sigma_bin: str, profile: str, get_token: Path, verbose: bool) -> None:
        self.sigma_bin = sigma_bin
        self.profile = profile
        self.get_token = get_token
        self.verbose = verbose

    def run(self, *args: str, retry_auth: bool = True) -> Any:
        cmd = [self.sigma_bin, "-p", self.profile, *args]
        if self.verbose:
            print("+ " + shlex.join(cmd), file=sys.stderr)
        from sigma_license_audit import wait_with_braille

        proc = wait_with_braille(
            "Sigma " + " ".join(args[:6]),
            lambda: subprocess.run(
                cmd,
                check=False,
                text=True,
                capture_output=True,
            ),
        )
        if proc.returncode == 2 and retry_auth:
            print("Sigma CLI auth failed; refreshing token and retrying once.", file=sys.stderr)
            refresh_token(self.get_token)
            return self.run(*args, retry_auth=False)
        if proc.returncode != 0:
            if proc.stderr:
                sys.stderr.write(proc.stderr)
            raise SigmaError(f"Sigma CLI failed: {shlex.join(cmd)}")
        if not proc.stdout.strip():
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise SigmaError(f"Sigma CLI returned malformed JSON for: {shlex.join(cmd)}") from exc

    def auth_status(self) -> bool:
        proc = subprocess.run(
            [self.sigma_bin, "-p", self.profile, "auth", "status"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0

    def api(
        self,
        *path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        args = ["api", *path]
        if params is not None:
            args.extend(["--params", json.dumps(params, separators=(",", ":"))])
        if body is not None:
            args.extend(["--json", json.dumps(body, separators=(",", ":"))])
        return self.run(*args)


def ensure_profile(args: argparse.Namespace, sigma: Sigma) -> None:
    if sigma.auth_status():
        return
    if not args.bootstrap.is_file():
        die(f"missing Sigma profile bootstrap: {args.bootstrap}")
    proc = subprocess.run(["python3", str(args.bootstrap), sigma.sigma_bin], check=False)
    if proc.returncode != 0:
        die(f"Sigma CLI profile {args.profile!r} could not be initialized")
    subprocess.run(
        [sigma.sigma_bin, "auth", "set-default", args.profile],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not sigma.auth_status():
        die(f"Sigma CLI authentication failed for profile {args.profile!r}")


def list_all(sigma: Sigma, *path: str, extra: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    page = ""
    while True:
        params: dict[str, Any] = {"limit": 1000}
        if extra:
            params.update(extra)
        if page:
            params["page"] = page
        response = sigma.api(*path, params=params)
        if isinstance(response, list):
            return response
        if not isinstance(response, dict):
            raise SigmaError(f"Unexpected list response from {' '.join(path)}")
        entries.extend(response.get("entries") or [])
        page = response.get("nextPage") or ""
        if not page:
            return entries


def member_from_json(raw: dict[str, Any]) -> Member:
    return Member(
        member_id=str(raw.get("memberId") or ""),
        email=str(raw.get("email") or ""),
        member_type=str(raw.get("memberType") or ""),
        home_folder_id=str(raw.get("homeFolderId") or ""),
        first_name=str(raw.get("firstName") or ""),
        last_name=str(raw.get("lastName") or ""),
        is_archived=bool(raw.get("isArchived")),
        is_inactive=bool(raw.get("isInactive")),
    )


def load_members(sigma: Sigma) -> list[Member]:
    raw_members = list_all(
        sigma,
        "members",
        "list",
        extra={"includeArchived": True, "includeInactive": True},
    )
    return [
        member
        for member in (member_from_json(raw) for raw in raw_members)
        if member.member_id and member.email
    ]


def resolve_member(query: str, members: list[Member], role: str) -> Member:
    q = query.strip().lower()
    exact = [
        m
        for m in members
        if q in {
            m.email.lower(),
            m.member_id.lower(),
            f"{m.first_name} {m.last_name}".strip().lower(),
        }
    ]
    matches = exact or [
        m
        for m in members
        if q in m.email.lower()
        or q in m.member_id.lower()
        or q in f"{m.first_name} {m.last_name}".strip().lower()
    ]
    if not matches:
        die(f"unknown {role} Sigma user: {query}")
    if len(matches) > 1:
        print(f"Ambiguous {role} user {query!r}; matches:", file=sys.stderr)
        for match in matches:
            print(f"  - {match.label}", file=sys.stderr)
        die(f"{role} user must match exactly one member", 2)
    return matches[0]


def account_type_names(sigma: Sigma) -> set[str]:
    try:
        rows: list[dict[str, Any]] = []
        page = ""
        while True:
            params: dict[str, Any] = {"pageSize": 1000}
            if page:
                params["pageToken"] = page
            response = sigma.api("account-types", "list", params=params)
            if isinstance(response, list):
                rows.extend(response)
                break
            if not isinstance(response, dict):
                raise SigmaError("Unexpected account-types list response")
            rows.extend(response.get("entries") or [])
            page = response.get("nextPage") or response.get("nextPageToken") or ""
            if not page:
                break
    except SigmaError as exc:
        print(f"WARNING: could not validate account types: {exc}", file=sys.stderr)
        return set()
    return {
        str(row.get("accountTypeName") or row.get("name"))
        for row in rows
        if row.get("accountTypeName") or row.get("name")
    }


def direct_children(sigma: Sigma, folder_id: str, include_archived: bool) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "parentId": folder_id,
        "directChildFilter": True,
        "limit": 1000,
    }
    if include_archived:
        params["includeArchived"] = True
    return list_all(sigma, "files", "list", extra=params)


def destination_name(args: argparse.Namespace, source: Member, multiple_sources: bool) -> str:
    base = args.destination_folder_name or (
        f"Migrated from {source.email} {datetime.now(UTC):%Y-%m-%d}"
    )
    if multiple_sources and args.destination_folder_name:
        return f"{base} - {source.email}"
    return base


def confirm_or_exit(args: argparse.Namespace) -> bool:
    if not args.execute:
        print("dry-run only; pass --execute to apply changes")
        return False
    if args.yes:
        return True
    if not sys.stdin.isatty():
        die("--execute requires --yes when stdin is not a TTY", 2)
    answer = input("Apply this Sigma downgrade and document migration? Type 'yes': ")
    if answer != "yes":
        raise SystemExit("aborted")
    return True


def main() -> int:
    args = parse_args()
    interactive = args.interactive or (not args.sources and not args.new_owner)
    if interactive and not sys.stdin.isatty():
        die("interactive mode requires a terminal; supply --user and --to instead", 2)
    if not interactive and (not args.sources or not args.new_owner):
        die("both --user and --to are required outside interactive mode", 2)

    os.environ["PATH"] = (
        f"{Path.home() / '.sigma-cli' / 'bin'}:/opt/homebrew/bin:/usr/local/bin:"
        f"{os.environ.get('PATH', '')}"
    )
    sigma_bin = resolve_sigma_bin(args.sigma_bin)

    load_env_file(args.sigma_env)
    for key in ("SIGMA_BASE_URL", "SIGMA_CLIENT_ID", "SIGMA_CLIENT_SECRET"):
        if not os.environ.get(key):
            die(f"{key} must be set in {args.sigma_env}")

    refresh_token(args.get_token)
    sigma = Sigma(
        sigma_bin=sigma_bin,
        profile=args.profile,
        get_token=args.get_token,
        verbose=args.verbose,
    )
    ensure_profile(args, sigma)
    sigma.api("whoami", "get")

    members = load_members(sigma)
    account_types = account_type_names(sigma)
    if interactive:
        interactive_setup(args, members, account_types)
        print("\nEquivalent command:")
        print(f"  {equivalent_command(args)}")

    target = resolve_member(args.new_owner, members, "destination")
    if target.is_archived or target.is_inactive:
        die(f"destination user must be active: {target.email}")
    if not target.home_folder_id:
        die(f"destination user has no homeFolderId: {target.email}")

    sources = [resolve_member(source, members, "source") for source in args.sources]
    if any(source.member_id == target.member_id for source in sources):
        die("source user and destination user must be different")

    if account_types and args.member_type not in account_types:
        print("Available account types:", file=sys.stderr)
        for name in sorted(account_types):
            print(f"  - {name}", file=sys.stderr)
        die(f"unknown --member-type: {args.member_type}", 2)

    multiple_sources = len(sources) > 1
    plan: list[dict[str, Any]] = []
    source_children: dict[str, list[dict[str, Any]]] = {}
    for source in sources:
        if source.is_archived or source.is_inactive:
            die(f"source user is not active: {source.email}")
        if not source.home_folder_id:
            die(f"source user has no homeFolderId: {source.email}")
        children = direct_children(sigma, source.home_folder_id, args.include_archived_files)
        source_children[source.member_id] = children
        plan.append(
            {
                "source": {
                    "email": source.email,
                    "memberId": source.member_id,
                    "currentMemberType": source.member_type,
                    "homeFolderId": source.home_folder_id,
                },
                "destination": {
                    "email": target.email,
                    "memberId": target.member_id,
                    "homeFolderId": target.home_folder_id,
                    "folderId": args.destination_folder_id or None,
                    "folderName": None
                    if args.destination_folder_id
                    else destination_name(args, source, multiple_sources),
                },
                "actions": {
                    "moveTopLevelDocuments": len(children),
                    "newOwnerId": target.member_id,
                    "newMemberType": args.member_type,
                    "execute": args.execute,
                },
                "files": [
                    {
                        "id": child.get("id"),
                        "name": child.get("name"),
                        "type": child.get("type"),
                        "path": child.get("path"),
                        "ownerId": child.get("ownerId"),
                    }
                    for child in children
                ],
            }
        )

    print("plan:")
    print(json.dumps(plan, indent=2, sort_keys=True))
    if not confirm_or_exit(args):
        return 0

    for source in sources:
        children = source_children[source.member_id]
        folder_id = args.destination_folder_id
        if not folder_id:
            folder = sigma.api(
                "files",
                "create",
                body={
                    "type": "folder",
                    "name": destination_name(args, source, multiple_sources),
                    "parentId": target.home_folder_id,
                    "ownerId": target.member_id,
                },
            )
            folder_id = str(folder.get("id") or "")
            if not folder_id:
                raise SigmaError("created destination folder but response did not include id")

        for child in children:
            inode_id = str(child.get("id") or "")
            if not inode_id:
                continue
            sigma.api(
                "files",
                "update",
                params={"inodeId": inode_id},
                body={"ownerId": target.member_id, "parentId": folder_id},
            )

        sigma.api(
            "members",
            "update",
            params={"memberId": source.member_id},
            body={"memberType": args.member_type},
        )
        print(
            f"migrated {len(children)} item(s) and set "
            f"{source.email} memberType to {args.member_type}"
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SigmaError as exc:
        die(str(exc))
