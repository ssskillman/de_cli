#!/usr/bin/env python3
"""Sigma License Management and Departed User Audit.

Cross-references Snowflake ADP Workforce Now employee termination records
against Sigma members, classifies license tiers (Build / Analyze / Act / View),
identifies departed users holding paid licenses, and supports dry-run or
executable downgrades and deactivations.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Re-exec into UDI virtualenv if snowflake.connector is not in current environment
def _maybe_reexec_into_udi_venv() -> None:
    if __name__ != "__main__":
        return
    if os.environ.get("_SIGMA_AUDIT_REEXEC") == "1":
        return
    try:
        import snowflake.connector  # noqa: F401
        return
    except ImportError:
        pass

    udi_py = Path.home() / "UDI" / ".venv" / "bin" / "python"
    if udi_py.is_file() and os.access(udi_py, os.X_OK):
        env = dict(os.environ)
        env["_SIGMA_AUDIT_REEXEC"] = "1"
        os.execve(str(udi_py), [str(udi_py), *sys.argv], env)


_maybe_reexec_into_udi_venv()

DEFAULT_SIGMA_ENV = Path.home() / ".config" / "sigma" / "env"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "de"
DEFAULT_CACHE_FILE = DEFAULT_CACHE_DIR / "adp_departed_workers.json"
DEFAULT_DBT_PROFILES = Path.home() / ".dbt" / "profiles.yml"

TIERS = ["View", "Act", "Analyze", "Build"]
PAID_TIERS = {"Build", "Analyze", "Act"}

BUILD_PERMS = {
    "admin", "annotate", "apply-tag", "badge", "contribute-to-shared-folders",
    "create-ai-column", "create-api-actions", "create-api-keys",
    "create-input-table", "create-stored-procedure-actions",
    "create-warehouse-view", "create-workspaces", "custom-fonts-and-workbook-themes",
    "develop-plugins", "edit-dashboard", "edit-dataset", "edit-report",
    "edit-workbook", "edit-worksheet", "embed", "export-as-email-burst",
    "export-to-cloud", "export-to-webhook", "join-data-sources", "manage-agents",
    "manage-api-connectors", "manage-branding-settings", "manage-connections",
    "manage-deployment-policies", "manage-email-branding",
    "manage-export-integrations", "manage-mcp-connectors", "manage-teams",
    "manage-user-attributes", "manage-users", "materialize", "share",
    "update-data-refresh", "update-table", "upload-csv", "use-mcp-connectors",
    "use-sigma-api-with-oauth", "version-tagging", "view-connections",
    "view-sql-in-usage-dashboards", "view-usage-dashboards", "write-python", "write-sql",
}

ANALYZE_PERMS = {
    "full-explore", "use-ai-formulas", "use-ai-formulas-assistant",
}

ACT_PERMS = {
    "basic-explore", "call-stored-procedure-actions", "edit-input-table",
    "export", "export-to-email", "export-to-google-drive", "export-to-google-sheet",
    "export-to-microsoft-teams", "export-to-microsoft-teams-and-share-point",
    "export-to-slack", "run-export-as-recipient", "schedule-export",
    "trigger-api-actions",
}


@dataclass(frozen=True)
class DepartedWorker:
    email: str
    full_name: str
    termination_date: str


@dataclass
class MemberAuditRecord:
    email: str
    member_id: str
    first_name: str
    last_name: str
    member_type: str
    license_type: str
    user_kind: str
    sigma_status: str  # active, inactive, deactivated
    is_archived: bool
    is_inactive: bool
    is_departed: bool
    termination_date: str = ""
    adp_name: str = ""
    is_paid: bool = False
    risk_level: str = "NONE"  # CRITICAL, HIGH, MEDIUM, LOW, CLEAN

    @property
    def full_name(self) -> str:
        name = f"{self.first_name} {self.last_name}".strip()
        return name or self.adp_name or self.email


def derive_tier_from_permissions(permissions: list[Any]) -> str:
    rank = 0
    for perm in permissions or []:
        if isinstance(perm, dict):
            perm = perm.get("permission") or ""
        perm = str(perm)
        if perm in BUILD_PERMS:
            return "Build"
        if perm in ANALYZE_PERMS:
            rank = max(rank, 2)
        elif perm in ACT_PERMS:
            rank = max(rank, 1)
    return TIERS[rank]


class SigmaClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        profile: str = "iterable",
        sigma_bin: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.profile = profile
        self.sigma_bin = sigma_bin or shutil.which("sigma")

    @classmethod
    def from_env(cls, env_path: Path = DEFAULT_SIGMA_ENV) -> SigmaClient:
        token = os.environ.get("SIGMA_API_TOKEN")
        base_url = os.environ.get("SIGMA_BASE_URL")
        client_id = os.environ.get("SIGMA_CLIENT_ID")
        client_secret = os.environ.get("SIGMA_CLIENT_SECRET")

        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k == "SIGMA_BASE_URL" and not base_url:
                    base_url = v
                elif k == "SIGMA_CLIENT_ID" and not client_id:
                    client_id = v
                elif k == "SIGMA_CLIENT_SECRET" and not client_secret:
                    client_secret = v
                elif k == "SIGMA_API_TOKEN" and not token:
                    token = v

        base_url = base_url or "https://aws-api.sigmacomputing.com"

        if not token:
            if not client_id or not client_secret:
                raise RuntimeError(
                    f"Missing Sigma credentials. Provide SIGMA_API_TOKEN or set "
                    f"SIGMA_CLIENT_ID and SIGMA_CLIENT_SECRET in {env_path}"
                )
            token = cls._mint_token(base_url, client_id, client_secret)

        return cls(base_url=base_url, token=token)

    @staticmethod
    def _mint_token(base_url: str, client_id: str, client_secret: str) -> str:
        cred = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        req = urllib.request.Request(
            f"{base_url.rstrip('/')}/v2/auth/token",
            data=b"grant_type=client_credentials",
            headers={
                "Authorization": f"Basic {cred}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                return data["access_token"]
        except urllib.error.HTTPError as exc:
            err = exc.read().decode(errors="replace")
            raise RuntimeError(f"Sigma token mint failed ({exc.code}): {err}") from exc

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
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            err = exc.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path} failed ({exc.code}): {err}") from exc
        if not payload:
            return None
        return json.loads(payload)

    def load_account_types_map(self) -> dict[str, str]:
        """Maps accountTypeName -> billed license tier ('Build', 'Analyze', 'Act', 'View')."""
        try:
            data = self.request("GET", "/v2/accountTypes")
        except Exception as exc:
            print(f"warn: could not load /v2/accountTypes ({exc}), using default mapping", file=sys.stderr)
            return {}

        entries = data.get("entries", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        type_to_tier: dict[str, str] = {}
        for entry in entries:
            name = entry.get("accountTypeName")
            if not name:
                continue
            lic_type = str(entry.get("licenseType") or "").strip().lower()
            if lic_type in ("build", "analyze", "act", "view"):
                type_to_tier[name] = lic_type.capitalize()
            else:
                account_type_id = entry.get("accountTypeId")
                if account_type_id:
                    try:
                        perms_data = self.request("GET", f"/v2/accountTypes/{account_type_id}/permissions")
                        type_to_tier[name] = derive_tier_from_permissions(perms_data)
                    except Exception:
                        type_to_tier[name] = "View"
                else:
                    type_to_tier[name] = "View"
        return type_to_tier

    def load_members(self) -> list[dict[str, Any]]:
        members: list[dict[str, Any]] = []
        page = None
        while True:
            params: dict[str, Any] = {
                "includeArchived": "true",
                "includeInactive": "true",
                "limit": 500,
            }
            if page:
                params["page"] = page
            data = self.request("GET", "/v2.1/members", params=params)
            entries = data.get("entries", []) if isinstance(data, dict) else []
            members.extend(entries)
            page = data.get("nextPage") if isinstance(data, dict) else None
            if not page:
                break
        return members

    def update_member_type(self, member_id: str, member_type: str = "View") -> Any:
        return self.request("PATCH", f"/v2/members/{member_id}", body={"memberType": member_type})

    def deactivate_member(
        self,
        member_id: str,
        new_owner_id: str | None = None,
        archive_docs: bool = False,
    ) -> Any:
        body: dict[str, Any] = {"isArchived": True}
        if archive_docs:
            body["archiveDocuments"] = True
        elif new_owner_id:
            body["newOwnerId"] = new_owner_id
        return self.request("PATCH", f"/v2/members/{member_id}", body=body)


class SnowflakeWorkerStore:
    SQL_DEPARTED = """
WITH worker_status AS (
    SELECT 
        LOWER(TRIM(b.EMAIL_URI)) as email,
        MAX(ph.LEGAL_NAME_FORMATTED_NAME) as full_name,
        MAX(w.TERMINATION_DATE) as max_term_date,
        BOOLOR_AGG(w.TERMINATION_DATE IS NULL OR w.TERMINATION_DATE > CURRENT_DATE()) as has_active_record,
        BOOLOR_AGG(w.TERMINATION_DATE IS NOT NULL AND w.TERMINATION_DATE <= CURRENT_DATE()) as has_terminated_record
    FROM FIVETRAN_DB.ADP_WORKFORCE_NOW.WORKER w
    JOIN FIVETRAN_DB.ADP_WORKFORCE_NOW.BUSINESS_COMMUNICATION b 
        ON b.WORKER_ID = w.ID AND b.TYPE = 'email'
    LEFT JOIN FIVETRAN_DB.ADP_WORKFORCE_NOW.PERSON_HISTORY ph 
        ON ph.WORKER_ID = w.ID
    WHERE b.EMAIL_URI IS NOT NULL AND b.EMAIL_URI LIKE '%@%'
    GROUP BY 1
)
SELECT 
    email,
    COALESCE(full_name, '') as full_name,
    max_term_date::text as termination_date
FROM worker_status
WHERE has_terminated_record AND NOT has_active_record
ORDER BY max_term_date DESC NULLS LAST;
""".strip()

    @staticmethod
    def load_cached(cache_file: Path = DEFAULT_CACHE_FILE, max_age_hours: int = 24) -> list[DepartedWorker] | None:
        if not cache_file.is_file():
            return None
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            ts = data.get("timestamp")
            if ts:
                cached_time = datetime.fromisoformat(ts)
                age = (datetime.now(timezone.utc) - cached_time).total_seconds() / 3600.0
                if age > max_age_hours:
                    return None
            records = data.get("workers", [])
            return [DepartedWorker(**r) for r in records]
        except Exception:
            return None

    @staticmethod
    def save_cache(workers: list[DepartedWorker], cache_file: Path = DEFAULT_CACHE_FILE) -> None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "count": len(workers),
            "workers": [asdict(w) for w in workers],
        }
        cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def fetch_from_snowflake(
        cls,
        profiles_path: Path = DEFAULT_DBT_PROFILES,
        use_cache: bool = True,
        force_refresh: bool = False,
    ) -> list[DepartedWorker]:
        if use_cache and not force_refresh:
            cached = cls.load_cached()
            if cached is not None:
                return cached

        try:
            import snowflake.connector
        except ImportError as exc:
            raise RuntimeError(
                "snowflake-connector-python is not installed in the active Python environment. "
                "Run via UDI environment: ~/UDI/.venv/bin/python scripts/sigma_license_audit.py"
            ) from exc

        params = cls._resolve_connection_params(profiles_path)
        try:
            conn = snowflake.connector.connect(**params)
            cur = conn.cursor()
            cur.execute(cls.SQL_DEPARTED)
            rows = cur.fetchall()
            conn.close()
        except Exception as exc:
            # If Snowflake fails but we have stale cache, return it with a warning
            cached = cls.load_cached(max_age_hours=999999)
            if cached:
                print(f"warn: Snowflake query failed ({exc}); falling back to cached worker roster", file=sys.stderr)
                return cached
            raise RuntimeError(f"Snowflake ADP query failed: {exc}") from exc

        workers = [
            DepartedWorker(
                email=str(row[0]).strip().lower(),
                full_name=str(row[1] or "").strip(),
                termination_date=str(row[2] or "").strip(),
            )
            for row in rows
            if row[0]
        ]
        cls.save_cache(workers)
        return workers

    @staticmethod
    def _resolve_connection_params(profiles_path: Path) -> dict[str, Any]:
        account = None
        user = None
        role = None
        warehouse = None
        authenticator = "externalbrowser"

        if profiles_path.is_file():
            try:
                import yaml
                data = yaml.safe_load(profiles_path.read_text(encoding="utf-8")) or {}
                udi = data.get("udi") or {}
                out = udi.get("outputs", {}).get(udi.get("target") or "dev", {})
                account = out.get("account")
                user = out.get("user")
                role = out.get("role")
                warehouse = out.get("warehouse")
                authenticator = out.get("authenticator") or authenticator
            except Exception:
                pass

        env_acc = os.environ.get("SNOWFLAKE_ACCOUNT") or os.environ.get("DE_SNOWFLAKE_ACCOUNT")
        if env_acc and env_acc != "your_account_identifier":
            account = env_acc
        env_user = os.environ.get("SNOWFLAKE_USER") or os.environ.get("DE_SNOWFLAKE_LOCAL_USER")
        if env_user and env_user != "your_user":
            user = env_user
        if os.environ.get("SNOWFLAKE_ROLE"):
            role = os.environ.get("SNOWFLAKE_ROLE")
        if os.environ.get("SNOWFLAKE_WAREHOUSE"):
            warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE")
        if os.environ.get("SNOWFLAKE_AUTHENTICATOR"):
            authenticator = os.environ.get("SNOWFLAKE_AUTHENTICATOR")

        if not account or not user:
            account = account or "PP13258-ITERABLE"
            user = user or "shawn.skillman@iterable.com"

        return {
            "account": account,
            "user": user,
            "role": role or "UDI_ENGINEER",
            "warehouse": warehouse or "AIRFLOWPROD",
            "database": "FIVETRAN_DB",
            "schema": "ADP_WORKFORCE_NOW",
            "authenticator": authenticator,
            "login_timeout": 30,
            "client_store_temporary_credential": True,
        }


def build_audit_records(
    raw_members: list[dict[str, Any]],
    departed_workers: list[DepartedWorker],
    type_to_tier: dict[str, str],
) -> list[MemberAuditRecord]:
    departed_by_email = {w.email: w for w in departed_workers}
    records: list[MemberAuditRecord] = []

    for raw in raw_members:
        email = str(raw.get("email") or "").strip().lower()
        if not email:
            continue

        member_type = str(raw.get("memberType") or "").strip()
        user_kind = str(raw.get("userKind") or "").strip().lower()

        # License tier lookup
        tier = type_to_tier.get(member_type)
        if not tier:
            clean_type = member_type.lower()
            if clean_type in ("build", "admin", "build+"):
                tier = "Build"
            elif clean_type in ("analyze",):
                tier = "Analyze"
            elif clean_type in ("act", "essential"):
                tier = "Act"
            else:
                tier = "View"

        is_archived = bool(raw.get("isArchived"))
        is_inactive = bool(raw.get("isInactive"))

        if is_archived:
            sigma_status = "deactivated"
        elif is_inactive:
            sigma_status = "inactive"
        else:
            sigma_status = "active"

        # Embed service accounts do not consume paid user licenses
        is_paid = (tier in PAID_TIERS) and (user_kind != "embed")

        departed_info = departed_by_email.get(email)
        is_departed = departed_info is not None

        # Risk level assessment
        if is_departed:
            if sigma_status == "active" and is_paid:
                risk_level = "CRITICAL"  # Active paid license for departed person
            elif sigma_status == "active":
                risk_level = "HIGH"      # Active view license for departed person
            elif is_paid:
                risk_level = "MEDIUM"    # Inactive/deactivated holding a paid type
            else:
                risk_level = "CLEAN"     # Deactivated and free
        else:
            risk_level = "LOW"

        record = MemberAuditRecord(
            email=email,
            member_id=str(raw.get("memberId") or ""),
            first_name=str(raw.get("firstName") or ""),
            last_name=str(raw.get("lastName") or ""),
            member_type=member_type,
            license_type=tier,
            user_kind=user_kind,
            sigma_status=sigma_status,
            is_archived=is_archived,
            is_inactive=is_inactive,
            is_departed=is_departed,
            termination_date=departed_info.termination_date if departed_info else "",
            adp_name=departed_info.full_name if departed_info else "",
            is_paid=is_paid,
            risk_level=risk_level,
        )
        records.append(record)

    return records


class AuditReporter:
    @staticmethod
    def render_summary(records: list[MemberAuditRecord]) -> str:
        total = len(records)
        active = sum(1 for r in records if r.sigma_status == "active")
        inactive = sum(1 for r in records if r.sigma_status == "inactive")
        deactivated = sum(1 for r in records if r.sigma_status == "deactivated")

        departed = [r for r in records if r.is_departed]
        departed_active_paid = [r for r in departed if r.sigma_status == "active" and r.is_paid]
        departed_active_free = [r for r in departed if r.sigma_status == "active" and not r.is_paid]
        departed_deactivated = [r for r in departed if r.sigma_status == "deactivated"]
        departed_inactive = [r for r in departed if r.sigma_status == "inactive"]

        paid_in_use = [r for r in records if r.sigma_status == "active" and r.is_paid]
        tier_counts = {t: sum(1 for r in paid_in_use if r.license_type == t) for t in ("Build", "Analyze", "Act")}
        reclaimable_tiers = {t: sum(1 for r in departed_active_paid if r.license_type == t) for t in ("Build", "Analyze", "Act")}

        lines = [
            "============================================================",
            "             SIGMA LICENSE & OFFBOARDING AUDIT              ",
            "============================================================",
            f"Total Sigma Members:        {total:4d}  (Active: {active}, Deactivated: {deactivated}, Inactive: {inactive})",
            f"Active Paid Seats in Use:   {len(paid_in_use):4d}  (Build: {tier_counts['Build']}, Analyze: {tier_counts['Analyze']}, Act: {tier_counts['Act']})",
            "------------------------------------------------------------",
            "Departed Employees in Sigma (via Snowflake ADP):",
            f"  Total Departed Found:     {len(departed):4d}",
            f"  [CRITICAL] Active + Paid: {len(departed_active_paid):4d}  <-- SEATS TO RECLAIM (wasting licenses)",
            f"  [HIGH]     Active + Free: {len(departed_active_free):4d}  <-- Active View accounts to deactivate",
            f"  [OK]       Deactivated:   {len(departed_deactivated):4d}",
            f"  [NOTE]     Inactive:      {len(departed_inactive):4d}",
            "------------------------------------------------------------",
            "Reclaimable Paid Licenses from Departed Staff:",
            f"  Build seats:              {reclaimable_tiers['Build']:4d}",
            f"  Analyze seats:            {reclaimable_tiers['Analyze']:4d}",
            f"  Act seats:                {reclaimable_tiers['Act']:4d}",
            f"  TOTAL RECLAIMABLE:        {len(departed_active_paid):4d}",
            "============================================================",
        ]
        return "\n".join(lines)

    @staticmethod
    def render_table(records: list[MemberAuditRecord]) -> str:
        if not records:
            return "(no matching members found)"

        sorted_records = sorted(records, key=lambda r: (r.full_name.casefold(), r.email.casefold()))
        headers = ["Email", "Name", "Tier", "Type", "Status", "Departed", "Term Date", "Risk"]
        rows = []
        for r in sorted_records:
            rows.append([
                r.email,
                r.full_name[:24],
                r.license_type,
                r.member_type[:14],
                r.sigma_status,
                "YES" if r.is_departed else "NO",
                r.termination_date or "-",
                r.risk_level,
            ])

        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths[i], len(cell))

        border = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"

        def format_row(cells: list[str]) -> str:
            return "| " + " | ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(cells)) + " |"

        lines = [border, format_row(headers), border]
        for row in rows:
            lines.append(format_row(row))
        lines.append(border)
        lines.append(f"Total matching: {len(records)}")
        return "\n".join(lines)

    @staticmethod
    def render_json(records: list[MemberAuditRecord], summary_only: bool = False) -> str:
        departed = [r for r in records if r.is_departed]
        departed_active_paid = [r for r in departed if r.sigma_status == "active" and r.is_paid]
        payload: dict[str, Any] = {
            "summary": {
                "total": len(records),
                "active": sum(1 for r in records if r.sigma_status == "active"),
                "deactivated": sum(1 for r in records if r.sigma_status == "deactivated"),
                "departedTotal": len(departed),
                "departedActivePaid": len(departed_active_paid),
                "departedActiveFree": sum(1 for r in departed if r.sigma_status == "active" and not r.is_paid),
                "reclaimableSeats": {
                    "Build": sum(1 for r in departed_active_paid if r.license_type == "Build"),
                    "Analyze": sum(1 for r in departed_active_paid if r.license_type == "Analyze"),
                    "Act": sum(1 for r in departed_active_paid if r.license_type == "Act"),
                },
            }
        }
        if not summary_only:
            payload["members"] = [asdict(r) for r in records]
        return json.dumps(payload, indent=2)

    @staticmethod
    def render_csv(records: list[MemberAuditRecord]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "email", "fullName", "licenseType", "memberType", "isPaid",
            "sigmaStatus", "isDeparted", "terminationDate", "riskLevel", "memberId"
        ])
        for r in records:
            writer.writerow([
                r.email, r.full_name, r.license_type, r.member_type, r.is_paid,
                r.sigma_status, r.is_departed, r.termination_date, r.risk_level, r.member_id
            ])
        return buf.getvalue().strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit Sigma licenses against Snowflake ADP termination records, identify departures, and reclaim paid seats."
    )
    # Output & filtering
    parser.add_argument("--summary", action="store_true", help="Print summary report only.")
    parser.add_argument("--format", choices=["table", "json", "csv"], default="table", help="Output format (default: table).")
    parser.add_argument("--all-members", action="store_true", help="Include all Sigma members, not just departed workers.")
    parser.add_argument("--paid-only", action="store_true", help="Filter to paid licenses (Build, Analyze, Act).")
    parser.add_argument("--active-only", action="store_true", help="Filter to active Sigma accounts.")
    parser.add_argument("--status", choices=["all", "active", "inactive", "deactivated"], default="all", help="Filter by Sigma status.")
    parser.add_argument("--search", help="Search filter by name or email.")
    parser.add_argument("--refresh", action="store_true", help="Force refresh Snowflake ADP worker cache.")
    parser.add_argument("--user", action="append", dest="users", help="Target specific user email(s).")

    # Remediation actions
    parser.add_argument("--downgrade-to-view", action="store_true", help="Downgrade targeted users to View account type.")
    parser.add_argument("--deactivate", action="store_true", help="Deactivate targeted users.")
    parser.add_argument("--to", "--new-owner", dest="new_owner", help="Email of active user receiving reassigned documents on deactivation.")
    parser.add_argument("--archive-docs", action="store_true", help="Archive documents instead of transferring to --new-owner.")
    parser.add_argument("--execute", action="store_true", help="Execute mutations (default is dry-run preview).")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt with --execute.")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive menu mode.")
    return parser.parse_args()


def run_remediation_plan(
    client: SigmaClient,
    targets: list[MemberAuditRecord],
    downgrade: bool,
    deactivate: bool,
    new_owner_email: str | None,
    archive_docs: bool,
    execute: bool,
    auto_confirm: bool,
) -> int:
    if not targets:
        print("No target members selected for remediation.")
        return 0

    new_owner_id = None
    if deactivate and not archive_docs:
        if not new_owner_email:
            print("ERROR: --deactivate requires --to <new_owner_email> or --archive-docs", file=sys.stderr)
            return 1
        # Resolve owner ID
        all_members = client.load_members()
        owner = next((m for m in all_members if str(m.get("email")).lower() == new_owner_email.lower()), None)
        if not owner:
            print(f"ERROR: Destination owner {new_owner_email!r} not found in Sigma.", file=sys.stderr)
            return 1
        if owner.get("isArchived") or owner.get("isInactive"):
            print(f"ERROR: Destination owner {new_owner_email!r} is not an active Sigma user.", file=sys.stderr)
            return 1
        new_owner_id = owner["memberId"]

    plan = []
    for t in targets:
        actions = []
        if downgrade and t.license_type != "View":
            actions.append("set memberType = View")
        if deactivate:
            actions.append(f"deactivate (docs -> {new_owner_email or 'archive'})")
        plan.append({
            "email": t.email,
            "memberId": t.member_id,
            "currentType": t.member_type,
            "currentTier": t.license_type,
            "actions": actions,
        })

    print("\nRemediation Plan:")
    print(json.dumps(plan, indent=2))
    print(f"\nTarget count: {len(plan)}")

    if not execute:
        print("\n[DRY-RUN ONLY] No changes were made. Pass --execute (and --yes) to apply.")
        return 0

    if not auto_confirm:
        if not sys.stdin.isatty():
            print("ERROR: Non-interactive execution requires --yes", file=sys.stderr)
            return 1
        ans = input(f"Apply changes to {len(plan)} user(s)? Type 'yes': ")
        if ans.strip().lower() != "yes":
            print("Aborted.")
            return 0

    # Execute
    for item in plan:
        mid = item["memberId"]
        email = item["email"]
        if downgrade:
            client.update_member_type(mid, "View")
            print(f"Downgraded {email} -> View")
        if deactivate:
            client.deactivate_member(mid, new_owner_id=new_owner_id, archive_docs=archive_docs)
            print(f"Deactivated {email}")

    print(f"\nSuccessfully executed remediation on {len(plan)} user(s).")
    return 0


def interactive_menu(records: list[MemberAuditRecord], client: SigmaClient) -> int:
    departed = [r for r in records if r.is_departed]
    departed_active_paid = [r for r in departed if r.sigma_status == "active" and r.is_paid]

    while True:
        print("\n" + AuditReporter.render_summary(records))
        print("\nChoose an option:")
        print("  1. List departed employees with active PAID licenses (CRITICAL)")
        print("  2. List all departed employees in Sigma")
        print("  3. Downgrade active departed paid users to View (dry-run preview)")
        print("  4. Deactivate active departed users (dry-run preview)")
        print("  5. Custom search user")
        print("  6. Export audit to CSV /tmp/sigma_departed_audit.csv")
        print("  q. Quit")

        choice = input("\nEnter choice [1-6, q]: ").strip()
        if choice in ("q", "quit", "exit"):
            return 0
        elif choice == "1":
            print(AuditReporter.render_table(departed_active_paid))
        elif choice == "2":
            print(AuditReporter.render_table(departed))
        elif choice == "3":
            run_remediation_plan(
                client=client,
                targets=departed_active_paid,
                downgrade=True,
                deactivate=False,
                new_owner_email=None,
                archive_docs=False,
                execute=False,
                auto_confirm=False,
            )
        elif choice == "4":
            owner = input("Enter destination email for reassigned documents: ").strip()
            if owner:
                run_remediation_plan(
                    client=client,
                    targets=departed_active_paid,
                    downgrade=False,
                    deactivate=True,
                    new_owner_email=owner,
                    archive_docs=False,
                    execute=False,
                    auto_confirm=False,
                )
        elif choice == "5":
            term = input("Search query: ").strip().lower()
            matches = [r for r in records if term in r.email.lower() or term in r.full_name.lower()]
            print(AuditReporter.render_table(matches))
        elif choice == "6":
            csv_path = Path("/tmp/sigma_departed_audit.csv")
            csv_path.write_text(AuditReporter.render_csv(records), encoding="utf-8")
            print(f"Exported {len(records)} records to {csv_path}")


def main() -> int:
    args = parse_args()

    # Initialize Sigma client
    try:
        sigma_client = SigmaClient.from_env()
    except Exception as exc:
        print(f"ERROR connecting to Sigma API: {exc}", file=sys.stderr)
        return 1

    # Fetch account types and members from Sigma
    try:
        type_to_tier = sigma_client.load_account_types_map()
        raw_members = sigma_client.load_members()
    except Exception as exc:
        print(f"ERROR querying Sigma API: {exc}", file=sys.stderr)
        return 1

    # Fetch departed employees from Snowflake ADP
    try:
        departed_workers = SnowflakeWorkerStore.fetch_from_snowflake(force_refresh=args.refresh)
    except Exception as exc:
        print(f"ERROR querying Snowflake ADP: {exc}", file=sys.stderr)
        return 1

    # Build audit records
    records = build_audit_records(raw_members, departed_workers, type_to_tier)

    # Interactive mode check
    if args.interactive or (len(sys.argv) == 1 and sys.stdin.isatty()):
        return interactive_menu(records, sigma_client)

    # Filtering
    filtered = records
    if not args.all_members and not args.users:
        filtered = [r for r in filtered if r.is_departed]

    if args.users:
        user_set = {u.strip().lower() for u in args.users}
        filtered = [r for r in filtered if r.email in user_set]

    if args.paid_only:
        filtered = [r for r in filtered if r.is_paid]

    if args.active_only:
        filtered = [r for r in filtered if r.sigma_status == "active"]

    if args.status != "all":
        filtered = [r for r in filtered if r.sigma_status == args.status]

    if args.search:
        q = args.search.lower()
        filtered = [r for r in filtered if q in r.email.lower() or q in r.full_name.lower()]

    # Remediation check
    if args.downgrade_to_view or args.deactivate:
        return run_remediation_plan(
            client=sigma_client,
            targets=filtered,
            downgrade=args.downgrade_to_view,
            deactivate=args.deactivate,
            new_owner_email=args.new_owner,
            archive_docs=args.archive_docs,
            execute=args.execute,
            auto_confirm=args.yes,
        )

    # Output rendering
    if args.summary:
        print(AuditReporter.render_summary(records))
        return 0

    if args.format == "json":
        print(AuditReporter.render_json(filtered))
    elif args.format == "csv":
        print(AuditReporter.render_csv(filtered))
    else:
        # Table output prints summary followed by the table
        print(AuditReporter.render_summary(records))
        print("\nFiltered Members:")
        print(AuditReporter.render_table(filtered))

    return 0


if __name__ == "__main__":
    sys.exit(main())
