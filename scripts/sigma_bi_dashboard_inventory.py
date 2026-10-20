#!/usr/bin/env python3
"""
BI-owned Sigma workbooks → JSON/CSV with endorsement + last-viewed.

REST (paginated, no MCP 20-cap):
  GET /v2/workbooks?skipPermissionCheck=true&excludeExplorations=true
  GET /v2/files?typeFilters=workbook
  GET /v2/members (emails)

Last viewed (not on REST): OBJECT_OPENED from Sigma audit-log connection SQL.
  Same query Sigma MCP uses; optional via --audit-sql or Snowflake env.

Auth: source ~/.config/sigma/env (SIGMA_BASE_URL, SIGMA_CLIENT_ID, SIGMA_CLIENT_SECRET)
      or set SIGMA_API_TOKEN directly.

Usage:
  python3 scripts/sigma_bi_dashboard_inventory.py
  python3 scripts/sigma_bi_dashboard_inventory.py -o bi-dashboards.json
  python3 scripts/sigma_bi_dashboard_inventory.py --format csv
  python3 scripts/sigma_bi_dashboard_inventory.py --skip-last-viewed
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# BI team (memberId from Sigma /v2/members)
BI_OWNERS: tuple[dict[str, str], ...] = (
    {
        "team_member": "Pratyush Adhwaryu",
        "member_id": "WYl7FjZwCaIrjwMqARyoUp9IAdxGR",
        "email": "pratyush.adhwaryu@iterable.com",
    },
    {
        "team_member": "Ashok Anandb",
        "member_id": "i378IAGFyGcMLCyZrc5DXj7CDjYCm",
        "email": "ashok.anandb@iterable.com",
    },
    {
        "team_member": "Rob Thornton",
        "member_id": "cFYgcMOAUUs0KViokX5R5Fp5b4DAT",
        "email": "robert.thornton@iterable.com",
    },
    {
        "team_member": "Shawn Skillman",
        "member_id": "yxi75ZDqu5tWpeoox9fGMpiL1yKsh",
        "email": "shawn.skillman@iterable.com",
    },
    {
        "team_member": "Aaron Yu",
        "member_id": "2T17QfV8e6PHyrXX2cpQZv03jfXtg",
        "email": "aaron.yu@iterable.com",
    },
)

OWNER_ID_TO_TEAM = {o["member_id"]: o for o in BI_OWNERS}

AUDIT_CONNECTION_ID = "f29be3b9-030a-4e07-a3a4-f3f820908247"
AUDIT_TABLE_INODE = "59444091-85c0-4e4e-9355-083e3050fa8c"

DEFAULT_SIGMA_ENV = Path.home() / ".config" / "sigma" / "env"
DEFAULT_GET_TOKEN = (
    Path.home() / "github" / "sigma-skills" / "sigma-api" / "scripts" / "get-token.sh"
)

COLUMNS = (
    "team_member",
    "dashboard",
    "dashboard_url",
    "endorsed",
    "owner_email",
    "last_viewed_utc",
    "last_viewed_by",
)


@dataclass(frozen=True)
class SigmaClient:
    base_url: str
    token: str

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        q = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{self.base_url}{path}"
        if q:
            url = f"{url}?{q}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        def send() -> dict[str, Any]:
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                raise SystemExit(f"Sigma API {path} failed HTTP {e.code}: {body}") from e

        from sigma_license_audit import wait_with_braille

        return wait_with_braille(f"Sigma GET {path}", send)

    def paginate(
        self, path: str, params: dict[str, Any], entries_key: str = "entries"
    ) -> Iterator[dict[str, Any]]:
        page_params = dict(params)
        while True:
            data = self.get_json(path, page_params)
            yield from data.get(entries_key) or []
            nxt = data.get("nextPage")
            if not nxt:
                break
            page_params["page"] = nxt


def load_dotenv_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("'\"")
        os.environ.setdefault(key, val)


def mint_token() -> str:
    if os.environ.get("SIGMA_API_TOKEN"):
        return os.environ["SIGMA_API_TOKEN"]
    env_file = Path(os.environ.get("SIGMA_ENV", DEFAULT_SIGMA_ENV))
    get_token = Path(os.environ.get("GET_TOKEN_SH", DEFAULT_GET_TOKEN))
    load_dotenv_file(env_file)
    for var in ("SIGMA_BASE_URL", "SIGMA_CLIENT_ID", "SIGMA_CLIENT_SECRET"):
        if not os.environ.get(var):
            raise SystemExit(
                f"Missing {var}. Set SIGMA_API_TOKEN or configure {env_file}"
            )
    if not get_token.is_file():
        raise SystemExit(f"Missing token script: {get_token}")
    from sigma_license_audit import wait_with_braille

    proc = wait_with_braille(
        "Sigma POST /v2/auth/token",
        lambda: subprocess.run(
            ["bash", str(get_token)],
            capture_output=True,
            text=True,
            check=False,
        ),
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr or proc.stdout or "get-token.sh failed")
    line = proc.stdout.strip()
    if not line.startswith("export SIGMA_API_TOKEN="):
        raise SystemExit("Unexpected get-token.sh output")
    # Safe parse: token is quoted by %q in get-token.sh
    token_part = line.split("=", 1)[1]
    if token_part.startswith("'") and token_part.endswith("'"):
        return token_part[1:-1]
    if token_part.startswith('"') and token_part.endswith('"'):
        return token_part[1:-1]
    return token_part


def endorsed_from_badge(badge: str | None) -> str:
    if not badge:
        return "no"
    if badge.lower() == "endorsed":
        return "yes"
    return badge


def fetch_owned_workbooks(client: SigmaClient, admin: bool) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "limit": 1000,
        "excludeExplorations": "true",
    }
    if admin:
        params["skipPermissionCheck"] = "true"
    rows: list[dict[str, Any]] = []
    for wb in client.paginate("/v2/workbooks", params):
        owner_id = wb.get("ownerId")
        if owner_id not in OWNER_ID_TO_TEAM:
            continue
        rows.append(wb)
    return rows


def fetch_badges_by_inode(client: SigmaClient) -> dict[str, str | None]:
    badges: dict[str, str | None] = {}
    params: dict[str, Any] = {"limit": 1000, "typeFilters": "workbook"}
    for f in client.paginate("/v2/files", params):
        inode_id = f.get("id")
        if inode_id:
            badges[inode_id] = f.get("badge")
    return badges


def audit_last_opens_sql(inode_ids: list[str]) -> str:
    """Postgres-syntax SQL for Sigma MCP connection query (not Snowflake QUALIFY)."""
    if not inode_ids:
        return ""
    in_list = ", ".join(f"'{i}'" for i in inode_ids)
    return f"""
WITH opens AS (
  SELECT
    "INODE_ID",
    "USER_EMAIL",
    "REQUEST_TIME",
    ROW_NUMBER() OVER (
      PARTITION BY "INODE_ID" ORDER BY "REQUEST_TIME" DESC
    ) AS rn
  FROM "connection"."{AUDIT_TABLE_INODE}"
  WHERE "EVENT_TYPE" = 'OBJECT_OPENED'
    AND "INODE_ID" IN ({in_list})
)
SELECT "INODE_ID", "USER_EMAIL", "REQUEST_TIME"
FROM opens
WHERE rn = 1
""".strip()


def fetch_last_viewed_snowflake(sql: str) -> dict[str, tuple[str, str]]:
    """Returns inode_id -> (timestamp_iso, user_email). Requires snow CLI."""
    if not sql:
        return {}
    if not shutil_which("snow"):
        print(
            "warn: snow CLI not found; last_viewed columns will be empty",
            file=sys.stderr,
        )
        return {}
    proc = subprocess.run(
        ["snow", "sql", "-q", sql, "--format", "JSON"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(
            f"warn: snow sql failed ({proc.returncode}): {proc.stderr[:500]}",
            file=sys.stderr,
        )
        return {}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print("warn: could not parse snow JSON output", file=sys.stderr)
        return {}
    out: dict[str, tuple[str, str]] = {}
    # snow JSON: list of dicts or wrapped — handle common shapes
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("data") or payload.get("rows") or []
    if not isinstance(rows, list):
        return {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        inode = row.get("INODE_ID") or row.get("inode_id")
        email = row.get("USER_EMAIL") or row.get("user_email") or ""
        ts = row.get("REQUEST_TIME") or row.get("request_time") or ""
        if inode:
            out[str(inode)] = (str(ts), str(email))
    return out


def parse_audit_json(path: Path) -> dict[str, tuple[str, str]]:
    """Merge last-viewed from Sigma MCP query JSON saved to a file."""
    raw = json.loads(path.read_text())
    out: dict[str, tuple[str, str]] = {}

    def ingest_dict(row: dict[str, Any]) -> None:
        inode = row.get("INODE_ID") or row.get("inode_id")
        email = row.get("USER_EMAIL") or row.get("user_email") or ""
        ts = row.get("REQUEST_TIME") or row.get("request_time") or ""
        if inode:
            out[str(inode)] = (str(ts), str(email))

    # Sigma MCP: {"columns":[...], "rows":[[...], ...]}
    if isinstance(raw, dict) and isinstance(raw.get("columns"), list) and isinstance(
        raw.get("rows"), list
    ):
        cols = [str(c) for c in raw["columns"]]
        for row in raw["rows"]:
            if isinstance(row, dict):
                ingest_dict(row)
            elif isinstance(row, list) and len(row) >= 3:
                ingest_dict(dict(zip(cols, row)))
        return out

    rows = raw
    if isinstance(raw, dict):
        rows = raw.get("rows") or raw.get("data") or raw.get("result") or []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if isinstance(row, dict):
            ingest_dict(row)
    return out


def shutil_which(cmd: str) -> str | None:
    from shutil import which

    return which(cmd)


def build_rows(
    workbooks: list[dict[str, Any]],
    badges: dict[str, str | None],
    last_viewed: dict[str, tuple[str, str]],
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for wb in sorted(workbooks, key=lambda w: (w.get("ownerId", ""), w.get("name", ""))):
        owner_id = wb.get("ownerId", "")
        team = OWNER_ID_TO_TEAM.get(owner_id, {})
        wb_id = wb.get("workbookId", "")
        lv = last_viewed.get(wb_id, ("", ""))
        badge = badges.get(wb_id)
        result.append(
            {
                "team_member": team.get("team_member", ""),
                "dashboard": wb.get("name", ""),
                "dashboard_url": wb.get("url", ""),
                "endorsed": endorsed_from_badge(badge),
                "owner_email": team.get("email", ""),
                "last_viewed_utc": lv[0],
                "last_viewed_by": lv[1],
            }
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write JSON/CSV to file (default: stdout)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--no-admin",
        action="store_true",
        help="Do not use skipPermissionCheck on /v2/workbooks",
    )
    parser.add_argument(
        "--skip-last-viewed",
        action="store_true",
        help="Skip audit-log lookup for last viewed",
    )
    parser.add_argument(
        "--audit-sql-out",
        type=Path,
        help="Write audit SQL to this path (for Sigma MCP query tool)",
    )
    parser.add_argument(
        "--audit-json",
        type=Path,
        help="JSON from Sigma MCP connection query (last viewed); skips snow",
    )
    args = parser.parse_args()

    load_dotenv_file(Path(os.environ.get("SIGMA_ENV", DEFAULT_SIGMA_ENV)))
    base = os.environ.get("SIGMA_BASE_URL", "").rstrip("/")
    if not base:
        raise SystemExit("SIGMA_BASE_URL is not set")

    token = mint_token()
    client = SigmaClient(base_url=base, token=token)

    workbooks = fetch_owned_workbooks(client, admin=not args.no_admin)
    badges = fetch_badges_by_inode(client)

    last_viewed: dict[str, tuple[str, str]] = {}
    inode_ids = [w["workbookId"] for w in workbooks if w.get("workbookId")]
    sql = audit_last_opens_sql(inode_ids)
    if args.audit_sql_out:
        args.audit_sql_out.write_text(sql + "\n")
        print(f"wrote audit SQL → {args.audit_sql_out}", file=sys.stderr)
    if not args.skip_last_viewed:
        if args.audit_json:
            last_viewed = parse_audit_json(args.audit_json)
        elif args.audit_sql_out:
            pass
        else:
            last_viewed = fetch_last_viewed_snowflake(sql)
            if not last_viewed and sql:
                print(
                    "tip: run with --audit-sql-out /tmp/bi-audit.sql, query via Sigma MCP, "
                    "then --audit-json /tmp/audit.json",
                    file=sys.stderr,
                )

    rows = build_rows(workbooks, badges, last_viewed)

    if args.format == "json":
        text = json.dumps(rows, indent=2) + "\n"
    else:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        text = buf.getvalue()

    if args.output:
        args.output.write_text(text)
        print(f"wrote {len(rows)} rows → {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
