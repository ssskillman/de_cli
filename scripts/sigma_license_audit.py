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
import threading
import time
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
DEFAULT_ACTIVITY_CACHE_FILE = DEFAULT_CACHE_DIR / "sigma_user_activity.json"
DEFAULT_DBT_PROFILES = Path.home() / ".dbt" / "profiles.yml"
GTM_MANAGER_EMAIL = "teri.hatfield@iterable.com"
GTM_TEAM_LABEL = "GTM Team (Teri Hatfield)"
VE_MANAGER_EMAIL = "katie.barends@iterable.com"
VE_TEAM_LABEL = "Value Engineering (Katie Barends)"

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


@dataclass(frozen=True)
class UserActivity:
    email: str
    last_activity: str = ""
    last_login: str = ""
    last_opened: str = ""
    last_edit: str = ""
    total_events: int = 0


@dataclass(frozen=True)
class ReportingWorker:
    email: str
    full_name: str
    depth: int
    manager_email: str


@dataclass(frozen=True)
class TeamLicenseRecord:
    email: str
    full_name: str
    depth: int
    manager_email: str
    relationship: str
    license_type: str
    member_type: str
    sigma_status: str


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
    created_at: str = ""
    days_since_created: int | None = None
    last_activity: str = ""
    last_login: str = ""
    last_opened: str = ""
    last_edit: str = ""
    total_events: int = 0
    utilization_status: str = "UNKNOWN"
    recommendation: str = ""

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

    def create_member(
        self,
        email: str,
        first_name: str,
        last_name: str,
        member_type: str = "View",
    ) -> Any:
        # POST /v2/members; sendInvite is a query param and defaults to emailing the user.
        # https://help.sigmacomputing.com/reference/create-member
        return self.request(
            "POST",
            "/v2/members",
            params={"sendInvite": "true"},
            body={
                "email": email,
                "firstName": first_name,
                "lastName": last_name,
                "memberType": member_type,
                "userKind": "internal",
            },
        )

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
WITH adp_workers AS (
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
),
active_adp_emails AS (
    SELECT email FROM adp_workers WHERE has_active_record
),
okta_terms AS (
    SELECT 
        LOWER(TRIM(PROFILE_EMAIL)) as email,
        MAX(TRIM(CONCAT(COALESCE(PROFILE_FIRST_NAME, ''), ' ', COALESCE(PROFILE_LAST_NAME, '')))) as full_name,
        MAX(STATUS_CHANGED::date) as max_term_date,
        BOOLOR_AGG(STATUS = 'ACTIVE' OR STATUS = 'SUSPENDED') as has_active_record,
        BOOLOR_AGG(STATUS = 'DEPROVISIONED') as has_terminated_record
    FROM FIVETRAN_DB.OKTA.USERS
    WHERE PROFILE_EMAIL IS NOT NULL AND PROFILE_EMAIL LIKE '%@%'
    GROUP BY 1
),
combined AS (
    -- Terminated workers from ADP without active record
    SELECT email, full_name, max_term_date 
    FROM adp_workers 
    WHERE has_terminated_record AND NOT has_active_record
    
    UNION ALL
    
    -- Deprovisioned users from Okta (excluding anyone with active ADP employment)
    SELECT email, full_name, max_term_date 
    FROM okta_terms 
    WHERE has_terminated_record 
      AND NOT has_active_record
      AND email NOT IN (SELECT email FROM active_adp_emails)
)
SELECT 
    email, 
    COALESCE(MAX(full_name), '') as full_name, 
    MAX(max_term_date)::text as termination_date 
FROM combined
GROUP BY email
ORDER BY 3 DESC NULLS LAST;
""".strip()

    SQL_REPORTING_TREE = """
WITH RECURSIVE
emails AS (
    SELECT
        WORKER_ID,
        LOWER(TRIM(EMAIL_URI)) AS email,
        ROW_NUMBER() OVER (
            PARTITION BY WORKER_ID
            ORDER BY COALESCE(NAME_EFFECTIVE_DATE, '1900-01-01') DESC, _FIVETRAN_SYNCED DESC
        ) AS rn
    FROM FIVETRAN_DB.ADP_WORKFORCE_NOW.BUSINESS_COMMUNICATION
    WHERE TYPE = 'email' AND EMAIL_URI IS NOT NULL
),
names AS (
    SELECT
        WORKER_ID,
        COALESCE(NULLIF(PREFERRED_NAME_FORMATTED_NAME, ''), LEGAL_NAME_FORMATTED_NAME) AS full_name,
        ROW_NUMBER() OVER (
            PARTITION BY WORKER_ID
            ORDER BY
                COALESCE(
                    PREFERRED_NAME_NAME_EFFECTIVE_DATE,
                    LEGAL_NAME_NAME_EFFECTIVE_DATE,
                    '1900-01-01'
                ) DESC,
                _FIVETRAN_SYNCED DESC
        ) AS rn
    FROM FIVETRAN_DB.ADP_WORKFORCE_NOW.PERSON_HISTORY
),
active_workers AS (
    SELECT
        w.ID AS worker_id,
        e.email,
        n.full_name
    FROM FIVETRAN_DB.ADP_WORKFORCE_NOW.WORKER w
    JOIN emails e ON e.WORKER_ID = w.ID AND e.rn = 1
    LEFT JOIN names n ON n.WORKER_ID = w.ID AND n.rn = 1
    WHERE (w.TERMINATION_DATE IS NULL OR w.TERMINATION_DATE > CURRENT_DATE())
      AND COALESCE(w._FIVETRAN_DELETED, FALSE) = FALSE
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY e.email
        ORDER BY w.STATUS_EFFECTIVE_DATE DESC NULLS LAST, w._FIVETRAN_SYNCED DESC
    ) = 1
),
reporting AS (
    SELECT WORKER_ID, REPORT_TO_WORKER_ID
    FROM FIVETRAN_DB.ADP_WORKFORCE_NOW.WORKER_REPORT_TO
    WHERE REPORT_TO_WORKER_ID IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY WORKER_ID
        ORDER BY
            REPORTS_TO_RELATIONSHIP_EFFECTIVE_DATE DESC NULLS LAST,
            _FIVETRAN_SYNCED DESC
    ) = 1
),
org (worker_id, email, full_name, manager_email, depth, worker_path) AS (
    SELECT
        w.worker_id,
        w.email,
        w.full_name,
        '',
        0,
        ARRAY_CONSTRUCT(w.worker_id)
    FROM active_workers w
    WHERE w.email = %s

    UNION ALL

    SELECT
        w.worker_id,
        w.email,
        w.full_name,
        org.email,
        org.depth + 1,
        ARRAY_APPEND(org.worker_path, w.worker_id)
    FROM org
    JOIN reporting r ON r.REPORT_TO_WORKER_ID = org.worker_id
    JOIN active_workers w ON w.worker_id = r.WORKER_ID
    WHERE NOT ARRAY_CONTAINS(w.worker_id::VARIANT, org.worker_path)
)
SELECT email, COALESCE(full_name, email), depth, manager_email
FROM org
ORDER BY depth, full_name, email;
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

    @classmethod
    def fetch_reporting_tree(
        cls,
        manager_email: str,
        profiles_path: Path = DEFAULT_DBT_PROFILES,
    ) -> list[ReportingWorker]:
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
            cur.execute(cls.SQL_REPORTING_TREE, (manager_email.strip().lower(),))
            rows = cur.fetchall()
            conn.close()
        except Exception as exc:
            raise RuntimeError(f"Snowflake ADP reporting hierarchy query failed: {exc}") from exc

        if not rows or int(rows[0][2]) != 0:
            raise RuntimeError(f"Active ADP manager not found: {manager_email}")
        return [
            ReportingWorker(
                email=str(row[0]).strip().lower(),
                full_name=str(row[1] or row[0]).strip(),
                depth=int(row[2]),
                manager_email=str(row[3] or "").strip().lower(),
            )
            for row in rows
            if row[0] and int(row[2]) > 0
        ]

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


class ActivityStore:
    @staticmethod
    def load_cached(cache_file: Path = DEFAULT_ACTIVITY_CACHE_FILE) -> dict[str, UserActivity]:
        if not cache_file.is_file():
            return {}
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            out: dict[str, UserActivity] = {}
            items = data.values() if isinstance(data, dict) else data
            for item in items:
                if not isinstance(item, dict):
                    continue
                email = str(item.get("EMAIL") or item.get("email") or "").strip().lower()
                if not email:
                    continue
                out[email] = UserActivity(
                    email=email,
                    last_activity=str(item.get("LAST_ACTIVITY") or item.get("last_activity") or "").strip(),
                    last_login=str(item.get("LAST_LOGIN") or item.get("last_login") or "").strip(),
                    last_opened=str(item.get("LAST_OPENED") or item.get("last_opened") or "").strip(),
                    last_edit=str(item.get("LAST_EDIT") or item.get("last_edit") or "").strip(),
                    total_events=int(item.get("TOTAL_EVENTS") or item.get("total_events") or 0),
                )
            return out
        except Exception as exc:
            print(f"warn: could not load activity cache {cache_file}: {exc}", file=sys.stderr)
            return {}

    @staticmethod
    def save_cache(activities: dict[str, UserActivity], cache_file: Path = DEFAULT_ACTIVITY_CACHE_FILE) -> None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: asdict(v) for k, v in activities.items()}
        cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def build_audit_records(
    raw_members: list[dict[str, Any]],
    departed_workers: list[DepartedWorker],
    type_to_tier: dict[str, str],
    activities: dict[str, UserActivity] | None = None,
) -> list[MemberAuditRecord]:
    if activities is None:
        activities = ActivityStore.load_cached()

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

        # Activity and Utilization classification
        act = (activities or {}).get(email)
        last_activity = act.last_activity if act else ""
        last_login = act.last_login if act else ""
        last_opened = act.last_opened if act else ""
        last_edit = act.last_edit if act else ""
        total_events = act.total_events if act else 0

        created_at = str(raw.get("createdAt") or "").strip()
        days_since_created = None
        if created_at:
            try:
                clean_c = created_at.replace("Z", "+00:00")
                dt_c = datetime.fromisoformat(clean_c)
                days_since_created = max(0, (datetime.now(timezone.utc) - dt_c).days)
            except Exception:
                pass

        if is_departed:
            utilization_status = "DEPARTED"
            recommendation = "Deactivate" if sigma_status == "active" else "—"
        elif not is_paid:
            utilization_status = "FREE_VIEW"
            recommendation = "—"
        elif sigma_status != "active":
            utilization_status = "INACTIVE_PAID"
            recommendation = "Downgrade to View"
        elif total_events == 0 or not last_activity:
            utilization_status = "DORMANT"
            recommendation = "Reclaim (Downgrade to View)"
        elif tier == "Build":
            if last_edit:
                utilization_status = "ACTIVE_BUILDER"
                recommendation = "Keep (Active Builder)"
            else:
                utilization_status = "UNDERUTILIZED_BUILD"
                recommendation = "Downgrade (View / Analyze)"
        else:
            utilization_status = "ACTIVE_ANALYST"
            recommendation = "Keep (Active Analyst)"

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
            created_at=created_at,
            days_since_created=days_since_created,
            last_activity=last_activity,
            last_login=last_login,
            last_opened=last_opened,
            last_edit=last_edit,
            total_events=total_events,
            utilization_status=utilization_status,
            recommendation=recommendation,
        )
        records.append(record)

    return records


def build_team_license_rows(
    workers: list[ReportingWorker],
    sigma_records: list[MemberAuditRecord],
) -> list[TeamLicenseRecord]:
    sigma_by_email = {record.email.casefold(): record for record in sigma_records}
    rows: list[TeamLicenseRecord] = []
    for worker in workers:
        sigma = sigma_by_email.get(worker.email.casefold())
        rows.append(
            TeamLicenseRecord(
                email=worker.email,
                full_name=worker.full_name,
                depth=worker.depth,
                manager_email=worker.manager_email,
                relationship="Direct" if worker.depth == 1 else "Indirect",
                license_type=sigma.license_type if sigma else "No Sigma account",
                member_type=sigma.member_type if sigma else "—",
                sigma_status=sigma.sigma_status if sigma else "—",
            )
        )
    return sorted(
        rows,
        key=lambda row: (
            0 if row.relationship == "Direct" else 1,
            row.depth,
            row.full_name.casefold(),
            row.email,
        ),
    )


def ascii_table(headers: list[str], rows: list[list[str]], numeric_columns: set[int] | None = None) -> str:
    numeric = numeric_columns or set()
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def format_row(cells: list[str]) -> str:
        formatted = []
        for index, cell in enumerate(cells):
            if index in numeric and cell != headers[index]:
                formatted.append(cell.rjust(widths[index]))
            else:
                formatted.append(cell.ljust(widths[index]))
        return "| " + " | ".join(formatted) + " |"

    lines = [border, format_row(headers), border]
    lines.extend(format_row(row) for row in rows)
    lines.append(border)
    return "\n".join(lines)


def render_team_employee_list(rows: list[TeamLicenseRecord]) -> str:
    people = ascii_table(
        ["#", "Relationship", "Depth", "Employee", "Email", "License", "Account type", "Status"],
        [
            [
                str(index),
                row.relationship,
                str(row.depth),
                row.full_name,
                row.email,
                row.license_type,
                row.member_type,
                row.sigma_status,
            ]
            for index, row in enumerate(rows, 1)
        ],
        numeric_columns={0, 2},
    )
    return "\n".join(["Employees", people])


def render_team_license_report(
    label: str,
    rows: list[TeamLicenseRecord],
    *,
    manager_email: str,
    include_employees: bool = True,
) -> str:
    tiers = ("Build", "Analyze", "Act", "View", "No Sigma account")

    def counts(selected: list[TeamLicenseRecord]) -> list[str]:
        return [str(sum(row.license_type == tier for row in selected)) for tier in tiers]

    direct = [row for row in rows if row.relationship == "Direct"]
    indirect = [row for row in rows if row.relationship == "Indirect"]
    summary = ascii_table(
        ["Group", "People", *tiers],
        [
            ["Direct", str(len(direct)), *counts(direct)],
            ["Indirect", str(len(indirect)), *counts(indirect)],
            ["Total", str(len(rows)), *counts(rows)],
        ],
        numeric_columns={1, 2, 3, 4, 5, 6},
    )
    parts = [
        label,
        f"Manager: {manager_email}",
        "",
        "License breakdown",
        summary,
    ]
    if include_employees:
        parts.extend(["", render_team_employee_list(rows)])
    return "\n".join(parts)


def offer_show_employee_list(
    rows: list[TeamLicenseRecord],
    *,
    input_fn=input,
    isatty: bool | None = None,
) -> str | None:
    """Ask before printing the employee table. Non-interactive runs skip the list."""
    if not rows:
        return None
    interactive = sys.stdin.isatty() if isatty is None else isatty
    if not interactive:
        return None
    answer = input_fn("\nShow the detailed user list? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        return None
    return render_team_employee_list(rows)


def render_reporting_team(
    records: list[MemberAuditRecord],
    *,
    label: str,
    manager_email: str,
) -> str:
    workers = SnowflakeWorkerStore.fetch_reporting_tree(manager_email)
    rows = build_team_license_rows(workers, records)
    return render_team_license_report(label, rows, manager_email=manager_email)


def render_gtm_team(records: list[MemberAuditRecord]) -> str:
    return render_reporting_team(
        records,
        label=GTM_TEAM_LABEL,
        manager_email=GTM_MANAGER_EMAIL,
    )


def render_value_engineering_team(records: list[MemberAuditRecord]) -> str:
    return render_reporting_team(
        records,
        label=VE_TEAM_LABEL,
        manager_email=VE_MANAGER_EMAIL,
    )


def split_person_name(full_name: str) -> tuple[str, str]:
    """ADP formatted names are 'Last, First Middle'."""
    name = " ".join(full_name.split())
    if "," in name:
        last, _, rest = name.partition(",")
        first = rest.strip()
        last = last.strip()
        if first and last:
            return first, last
        token = first or last or "Unknown"
        return token, token
    parts = name.split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    if parts:
        return parts[0], parts[0]
    return "Unknown", "Unknown"


def resolve_view_account_type(type_to_tier: dict[str, str]) -> str:
    for name, tier in type_to_tier.items():
        if str(tier) == "View" and str(name).casefold() == "view":
            return str(name)
    for name, tier in type_to_tier.items():
        if str(tier) == "View" and str(name).strip():
            return str(name)
    return "View"


def resolve_act_plus_account_type(type_to_tier: dict[str, str]) -> str:
    """Iterable's Act+ account type is the Act license. The account type named Act is Build."""
    for name, tier in type_to_tier.items():
        if str(name).casefold() == "act+" and str(tier) == "Act":
            return str(name)
    for name in type_to_tier:
        if str(name).casefold() == "act+":
            return str(name)
    return "Act+"


def account_type_tiers(client: SigmaClient, type_to_tier: dict[str, str] | None) -> dict[str, str]:
    tiers = dict(type_to_tier or {})
    if not tiers and hasattr(client, "load_account_types_map"):
        tiers = client.load_account_types_map()
    return tiers


def offer_missing_view_accounts(
    *,
    workers: list[ReportingWorker],
    records: list[MemberAuditRecord],
    client: SigmaClient,
    type_to_tier: dict[str, str] | None,
    label: str,
    manager_email: str,
    input_fn=input,
    isatty: bool | None = None,
) -> str | None:
    """Ask whether people with no Sigma account should be created as View."""
    missing = [
        row
        for row in build_team_license_rows(workers, records)
        if row.license_type == "No Sigma account"
    ]
    if not missing:
        return None
    interactive = sys.stdin.isatty() if isatty is None else isatty
    if not interactive:
        return None
    answer = input_fn(
        f"\nAdd the {len(missing)} people with no Sigma account with "
        "View license / View account type? [y/N] "
    ).strip().lower()
    if answer not in {"y", "yes"}:
        return None

    tiers = account_type_tiers(client, type_to_tier)
    member_type = resolve_view_account_type(tiers)
    created = 0
    failures: list[str] = []
    for row in missing:
        first_name, last_name = split_person_name(row.full_name)
        try:
            client.create_member(row.email, first_name, last_name, member_type)
            created += 1
        except Exception as exc:
            failures.append(f"{row.email}: {exc}")

    refreshed = build_audit_records(client.load_members(), [], tiers, activities={})
    report = render_team_license_report(
        label,
        build_team_license_rows(workers, refreshed),
        manager_email=manager_email,
        include_employees=False,
    )
    lines = [f"Created {created} View account(s)."]
    if failures:
        lines.append("Failed:")
        lines.extend(f"  {item}" for item in failures)
    lines.extend(["", "Updated license breakdown", report])
    return "\n".join(lines)


def offer_upgrade_view_to_act_plus(
    *,
    workers: list[ReportingWorker],
    records: list[MemberAuditRecord],
    client: SigmaClient,
    type_to_tier: dict[str, str] | None,
    label: str,
    manager_email: str,
    input_fn=input,
    isatty: bool | None = None,
) -> str | None:
    """Ask whether View team members should be upgraded to the Act+ account type."""
    by_email = {record.email.casefold(): record for record in records}
    targets = []
    for row in build_team_license_rows(workers, records):
        if row.license_type != "View":
            continue
        record = by_email.get(row.email.casefold())
        if record and record.member_id:
            targets.append(record)
    if not targets:
        return None
    interactive = sys.stdin.isatty() if isatty is None else isatty
    if not interactive:
        return None
    answer = input_fn(
        f"\nUpgrade {len(targets)} team members from View to Act+? [y/N] "
    ).strip().lower()
    if answer not in {"y", "yes"}:
        return None

    tiers = account_type_tiers(client, type_to_tier)
    member_type = resolve_act_plus_account_type(tiers)
    upgraded = 0
    failures: list[str] = []
    for record in targets:
        try:
            client.update_member_type(record.member_id, member_type)
            upgraded += 1
        except Exception as exc:
            failures.append(f"{record.email}: {exc}")

    refreshed = build_audit_records(client.load_members(), [], tiers, activities={})
    report = render_team_license_report(
        label,
        build_team_license_rows(workers, refreshed),
        manager_email=manager_email,
        include_employees=False,
    )
    lines = [f"Upgraded {upgraded} account(s) from View to {member_type}."]
    if failures:
        lines.append("Failed:")
        lines.extend(f"  {item}" for item in failures)
    lines.extend(["", "Updated license breakdown", report])
    return "\n".join(lines)


def run_reporting_team_prompt(
    records: list[MemberAuditRecord],
    client: SigmaClient,
    label: str,
    manager_email: str,
    type_to_tier: dict[str, str] | None = None,
) -> None:
    workers = SnowflakeWorkerStore.fetch_reporting_tree(manager_email)
    rows = build_team_license_rows(workers, records)
    print(
        render_team_license_report(
            label,
            rows,
            manager_email=manager_email,
            include_employees=False,
        )
    )
    listed = offer_show_employee_list(rows)
    if listed:
        print("\n" + listed)
    updated = offer_missing_view_accounts(
        workers=workers,
        records=records,
        client=client,
        type_to_tier=type_to_tier,
        label=label,
        manager_email=manager_email,
    )
    current_records = records
    current_tiers = type_to_tier
    if updated:
        print("\n" + updated)
        current_tiers = account_type_tiers(client, type_to_tier)
        current_records = build_audit_records(
            client.load_members(), [], current_tiers, activities={}
        )
        listed = offer_show_employee_list(build_team_license_rows(workers, current_records))
        if listed:
            print("\n" + listed)
    upgraded = offer_upgrade_view_to_act_plus(
        workers=workers,
        records=current_records,
        client=client,
        type_to_tier=current_tiers,
        label=label,
        manager_email=manager_email,
    )
    if upgraded:
        print("\n" + upgraded)
        final_tiers = account_type_tiers(client, current_tiers)
        final_records = build_audit_records(
            client.load_members(), [], final_tiers, activities={}
        )
        listed = offer_show_employee_list(build_team_license_rows(workers, final_records))
        if listed:
            print("\n" + listed)


def format_ts_date(ts: str) -> str:
    if not ts:
        return "Never"
    clean = ts.replace("Z", "").replace("T", " ")
    if " " in clean:
        return clean.split(" ", 1)[0]
    return clean[:10]


def format_days_elapsed(days: int | None) -> str:
    if days is None:
        return "-"
    return f"{days}d"


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

        # Utilization metrics for active paid seats
        active_builders = sum(1 for r in paid_in_use if r.utilization_status == "ACTIVE_BUILDER")
        active_analysts = sum(1 for r in paid_in_use if r.utilization_status == "ACTIVE_ANALYST")
        underutilized_builders = sum(1 for r in paid_in_use if r.utilization_status == "UNDERUTILIZED_BUILD")
        dormant_paid = sum(1 for r in paid_in_use if r.utilization_status == "DORMANT")
        departed_paid = sum(1 for r in paid_in_use if r.utilization_status == "DEPARTED")

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
            "------------------------------------------------------------",
            "Paid License Utilization & Optimization (Last 30 Days):",
            f"  Effectively Utilized:     {active_builders + active_analysts:4d}  (Builders: {active_builders}, Analysts: {active_analysts})",
            f"  [OPPORTUNITY] View-Only Builders: {underutilized_builders:4d}  <-- Build tier with 0 edits (downgrade to View/Analyze)",
            f"  [OPPORTUNITY] Dormant Paid Seats: {dormant_paid:4d}  <-- 0 activity in 30d (reclaim candidate)",
            f"  [CRITICAL]    Departed Paid Seats:{departed_paid:4d}  <-- Departed employees holding paid seats",
            f"  TOTAL OPTIMIZATION TARGETS:       {underutilized_builders + dormant_paid + departed_paid:4d}",
            "============================================================",
        ]
        return "\n".join(lines)

    @staticmethod
    def render_table(records: list[MemberAuditRecord], utilization_mode: bool = False) -> str:
        if not records:
            return "(no matching members found)"

        if utilization_mode:
            def util_sort_key(r: MemberAuditRecord):
                is_never = not r.last_activity or r.last_activity.strip().lower() == "never"
                if is_never:
                    return (0, 0.0, r.email.casefold())

                try:
                    date_str = r.last_activity.strip().split()[0].split("T")[0]
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                    epoch = dt.timestamp()
                except Exception:
                    epoch = 0.0

                return (1, -epoch, r.email.casefold())

            sorted_records = sorted(records, key=util_sort_key)
            headers = ["#", "Email", "Name", "Tier", "Type", "Invited", "Days", "Last Active", "Last Edit", "Events", "Utilization", "Recommendation"]
            rows = []
            for idx, r in enumerate(sorted_records, 1):
                rows.append([
                    str(idx),
                    r.email,
                    r.full_name[:22],
                    r.license_type,
                    r.member_type[:12],
                    format_ts_date(r.created_at),
                    format_days_elapsed(r.days_since_created),
                    format_ts_date(r.last_activity),
                    format_ts_date(r.last_edit),
                    str(r.total_events),
                    r.utilization_status,
                    r.recommendation,
                ])
        else:
            sorted_records = sorted(records, key=lambda r: (r.full_name.casefold(), r.email.casefold()))
            headers = ["#", "Email", "Name", "Tier", "Type", "Status", "Departed", "Term Date", "Risk"]
            rows = []
            for idx, r in enumerate(sorted_records, 1):
                rows.append([
                    str(idx),
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
            formatted = []
            for i, cell in enumerate(cells):
                if i == 0 and cell != "#":
                    formatted.append(cell.rjust(col_widths[i]))
                else:
                    formatted.append(cell.ljust(col_widths[i]))
            return "| " + " | ".join(formatted) + " |"

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
        paid_in_use = [r for r in records if r.sigma_status == "active" and r.is_paid]
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
                "utilization": {
                    "activePaidTotal": len(paid_in_use),
                    "activeBuilders": sum(1 for r in paid_in_use if r.utilization_status == "ACTIVE_BUILDER"),
                    "activeAnalysts": sum(1 for r in paid_in_use if r.utilization_status == "ACTIVE_ANALYST"),
                    "underutilizedBuilders": sum(1 for r in paid_in_use if r.utilization_status == "UNDERUTILIZED_BUILD"),
                    "dormantPaid": sum(1 for r in paid_in_use if r.utilization_status == "DORMANT"),
                    "departedPaid": sum(1 for r in paid_in_use if r.utilization_status == "DEPARTED"),
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
            "sigmaStatus", "isDeparted", "terminationDate", "riskLevel",
            "invitedAt", "daysElapsed",
            "lastActivity", "lastLogin", "lastOpened", "lastEdit", "totalEvents",
            "utilizationStatus", "recommendation", "memberId"
        ])
        for r in records:
            writer.writerow([
                r.email, r.full_name, r.license_type, r.member_type, r.is_paid,
                r.sigma_status, r.is_departed, r.termination_date, r.risk_level,
                r.created_at, r.days_since_created if r.days_since_created is not None else "",
                r.last_activity, r.last_login, r.last_opened, r.last_edit, r.total_events,
                r.utilization_status, r.recommendation, r.member_id
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
    parser.add_argument("--activity-file", type=Path, default=DEFAULT_ACTIVITY_CACHE_FILE, help="Path to Sigma user activity cache JSON.")
    parser.add_argument("--utilization", action="store_true", help="Show paid license utilization and activity table.")
    parser.add_argument("--dormant-only", action="store_true", help="Filter to dormant paid users (0 activity in 30 days).")
    parser.add_argument("--underutilized-only", action="store_true", help="Filter to view-only Builders (0 edits in 30 days).")
    parser.add_argument("--account-type", action="append", dest="account_types", help="Filter by Sigma account type (e.g. Act, build, admin, View). Repeatable.")
    parser.add_argument("--tier", "--license-type", action="append", dest="license_types", help="Filter by Sigma billed license tier (Build, Analyze, Act, View). Repeatable.")
    parser.add_argument("--employee-type", action="append", dest="employee_types", choices=["active", "departed", "guest", "embed", "all"], help="Filter by employee type (active, departed, guest, embed, all). Repeatable.")
    parser.add_argument(
        "--gtm-team",
        action="store_true",
        help="List GTM Team (Teri Hatfield), direct and indirect reports, with Sigma license breakdown.",
    )
    parser.add_argument(
        "--value-engineering",
        action="store_true",
        help="List Value Engineering (Katie Barends), direct and indirect reports, with Sigma license breakdown.",
    )

    # Remediation actions
    parser.add_argument("--downgrade-to-view", action="store_true", help="Downgrade targeted users to View account type.")
    parser.add_argument("--deactivate", action="store_true", help="Deactivate targeted users.")
    parser.add_argument("--to", "--new-owner", dest="new_owner", help="Email of active user receiving reassigned documents on deactivation.")
    parser.add_argument("--archive-docs", action="store_true", help="Archive documents instead of transferring to --new-owner.")
    parser.add_argument("--execute", action="store_true", help="Execute mutations (default is dry-run preview).")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt with --execute.")
    parser.add_argument("--one-by-one", action="store_true", help="Prompt to confirm remediation one user at a time.")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive menu mode.")
    return parser.parse_args()


class BrailleSpinner:
    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, message: str = "") -> None:
        self.message = message
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not sys.stderr.isatty():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def update(self, message: str) -> None:
        self.message = message

    def _spin(self) -> None:
        idx = 0
        sys.stderr.write("\033[?25l")
        try:
            while not self._stop_event.is_set():
                frame = self.FRAMES[idx % len(self.FRAMES)]
                sys.stderr.write(f"\r\033[K{frame} {self.message}")
                sys.stderr.flush()
                idx += 1
                time.sleep(0.08)
        finally:
            sys.stderr.write("\r\033[K\033[?25h")
            sys.stderr.flush()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.5)
            self._thread = None
        if sys.stderr.isatty():
            sys.stderr.write("\r\033[K\033[?25h")
            sys.stderr.flush()


def fzf_pick_choice(
    entries: list[tuple[str, str]],
    *,
    prompt: str = "action > ",
    header: str = "",
    height: str = "40%",
) -> str | None:
    if not entries:
        return None
    if shutil.which("fzf") and sys.stdin.isatty():
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
        proc = subprocess.run(
            cmd,
            input="\n".join(lines) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.split("\t", 1)[0].strip()
        return None

    # Numbered fallback if fzf/tty unavailable
    if header:
        print(f"\n{header}")
    for idx, (key, label) in enumerate(entries, 1):
        print(f"  {label}")
    ans = input(f"\n{prompt}").strip().lower()
    if ans in {k.lower() for k, _ in entries}:
        return next(k for k, _ in entries if k.lower() == ans)
    if ans.isdigit() and 1 <= int(ans) <= len(entries):
        return entries[int(ans) - 1][0]
    return None


def fzf_pick_multi(
    entries: list[tuple[str, str]],
    *,
    prompt: str = "select > ",
    header: str = "",
    height: str = "45%",
) -> list[str]:
    if not entries:
        return []
    if shutil.which("fzf") and sys.stdin.isatty():
        lines = [f"{key}\t{label}" for key, label in entries]
        cmd = [
            "fzf",
            "--ansi",
            "--no-mouse",
            "--multi",
            "--bind=ctrl-a:select-all,ctrl-d:deselect-all",
            f"--height={height}",
            "--reverse",
            "--border=rounded",
            "--cycle",
            "--delimiter=\t",
            "--with-nth=2",
            f"--prompt={prompt}",
            "--pointer=▶",
            "--marker=◉ ",
            f"--header={header}",
            "--footer=Tab=toggle · Ctrl-A=all · Enter=confirm · Esc=cancel",
        ]
        proc = subprocess.run(
            cmd,
            input="\n".join(lines) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            selected = []
            for line in proc.stdout.splitlines():
                if "\t" in line:
                    selected.append(line.split("\t", 1)[0].strip())
                elif line.strip():
                    selected.append(line.strip())
            return selected
        return []

    # Numbered fallback if fzf/tty unavailable
    if header:
        print(f"\n{header}")
    for idx, (key, label) in enumerate(entries, 1):
        print(f"  {idx}. {label}")
    print("  (comma-separated numbers or 'all', empty to cancel)")
    ans = input(f"\n{prompt}").strip().lower()
    if not ans or ans in ("q", "quit", "cancel", "esc"):
        return []
    if ans == "all":
        return [k for k, _ in entries]
    chosen = []
    for part in ans.split(","):
        p = part.strip()
        if p.isdigit() and 1 <= int(p) <= len(entries):
            chosen.append(entries[int(p) - 1][0])
        elif p in {k.lower() for k, _ in entries}:
            chosen.append(next(k for k, _ in entries if k.lower() == p))
    return chosen


def run_remediation_plan(
    client: SigmaClient,
    targets: list[MemberAuditRecord],
    downgrade: bool,
    deactivate: bool,
    new_owner_email: str | None,
    archive_docs: bool,
    execute: bool,
    auto_confirm: bool,
    one_by_one: bool = False,
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

    # One-by-one interactive confirmation flow
    if one_by_one:
        mode_str = "LIVE EXECUTE" if execute else "DRY-RUN PREVIEW"
        dest_str = "archive" if archive_docs else (new_owner_email or "none")
        print(f"\nOne-by-one remediation [{mode_str}] (docs -> {dest_str}):")
        count = 0
        for idx, t in enumerate(targets, 1):
            tier_info = f"{t.license_type} ({t.member_type})"
            term_info = f"Terminated: {t.termination_date}" if t.termination_date else "Departed"
            actions = []
            if downgrade and t.license_type != "View":
                actions.append("downgrade to View")
            if deactivate:
                actions.append(f"deactivate (docs -> {dest_str})")
            action_desc = " & ".join(actions) or "no-op"

            print(f"\n[{idx}/{len(targets)}] {t.full_name} <{t.email}> [{tier_info}, {term_info}]")
            ans = input(f"Action: {action_desc}? [y/N/q]: ").strip().lower()
            if ans in ("q", "quit", "exit"):
                print("Exited one-by-one flow.")
                break
            if ans in ("y", "yes"):
                if execute:
                    if downgrade:
                        client.update_member_type(t.member_id, "View")
                        t.member_type = "View"
                        t.license_type = "View"
                        t.is_paid = False
                        print(f"  ✓ Downgraded {t.email} -> View")
                    if deactivate:
                        client.deactivate_member(t.member_id, new_owner_id=new_owner_id, archive_docs=archive_docs)
                        t.sigma_status = "deactivated"
                        t.is_archived = True
                        t.is_paid = False
                        t.risk_level = "CLEAN"
                        print(f"  ✓ Deactivated {t.email}")
                else:
                    print(f"  [DRY-RUN] Simulated: {action_desc} for {t.email}")
                count += 1
            else:
                print("  Skipped.")

        print(f"\nCompleted one-by-one flow. Processed {count} user(s).")
        return 0

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

    # Execute batch
    tier_counts = {"Build": 0, "Analyze": 0, "Act": 0}
    succeeded = 0
    failed = 0
    errors: list[str] = []

    spinner = BrailleSpinner()
    spinner.start()
    try:
        for idx, item in enumerate(plan, 1):
            mid = item["memberId"]
            email = item["email"]
            current_tier = item["currentTier"]
            t = next((x for x in targets if x.member_id == mid), None)
            action_desc = "Downgrading" if downgrade else "Deactivating"
            spinner.update(f"{action_desc} {email} ({idx}/{len(plan)})...")
            try:
                if downgrade:
                    client.update_member_type(mid, "View")
                    if t:
                        t.member_type = "View"
                        t.license_type = "View"
                        t.is_paid = False
                        t.utilization_status = "FREE_VIEW"
                        t.recommendation = "—"
                    if current_tier in tier_counts:
                        tier_counts[current_tier] += 1
                    print(f"Downgraded {email} -> View")
                if deactivate:
                    client.deactivate_member(mid, new_owner_id=new_owner_id, archive_docs=archive_docs)
                    if t:
                        t.sigma_status = "deactivated"
                        t.is_archived = True
                        t.is_paid = False
                        t.risk_level = "CLEAN"
                        t.utilization_status = "DEPARTED"
                        t.recommendation = "—"
                    print(f"Deactivated {email}")
                succeeded += 1
            except Exception as exc:
                failed += 1
                errors.append(f"{email}: {exc}")
                print(f"ERROR: failed on {email}: {exc}", file=sys.stderr)
    finally:
        spinner.stop()

    action_title = "Downgrade to View" if downgrade else "Deactivate Member"
    lines = [
        "",
        "============================================================",
        "              REMEDIATION EXECUTION SUMMARY                 ",
        "============================================================",
        f"Action:                   {action_title}",
        f"Mode:                     Full Prod Send It",
        f"Total Targeted:           {len(plan)}",
        f"Successfully Completed:   {succeeded}",
        f"Failed:                   {failed}",
    ]
    if downgrade and succeeded > 0:
        lines.extend([
            "------------------------------------------------------------",
            "Reclaimed Paid Seats by Tier:",
            f"  Build -> View:          {tier_counts['Build']} seats",
            f"  Analyze -> View:        {tier_counts['Analyze']} seats",
            f"  Act -> View:            {tier_counts['Act']} seats",
            f"  TOTAL RECLAIMED:        {sum(tier_counts.values())} seats",
        ])
    if errors:
        lines.extend([
            "------------------------------------------------------------",
            "Errors encountered:",
            *[f"  • {err}" for err in errors[:5]],
        ])
    lines.append("============================================================")
    print("\n".join(lines))
    return 0 if failed == 0 else 1


def interactive_deactivate_one_by_one(records: list[MemberAuditRecord], client: SigmaClient) -> None:
    sel = fzf_pick_choice(
        [
            ("1", "1. Active departed users with PAID licenses (CRITICAL seats)"),
            ("2", "2. Pick specific user by email or name"),
            ("3", "3. All active departed users in Sigma"),
            ("q", "q. Cancel"),
        ],
        prompt="target > ",
        header="Select target group for one-by-one deactivation · pick option",
        height="35%",
    )
    if not sel or sel in ("q", "quit", "exit"):
        return

    if sel == "1":
        targets = [r for r in records if r.is_departed and r.sigma_status == "active" and r.is_paid]
    elif sel == "2":
        q = input("\nSearch email or name: ").strip().lower()
        if not q:
            return
        matches = [r for r in records if q in r.email.lower() or q in r.full_name.lower()]
        if not matches:
            print("No matching users found.")
            return
        if len(matches) == 1:
            targets = matches
        else:
            user_entries = [
                (m.email, f"{m.full_name:<24}  <{m.email}>  [{m.license_type}, {m.sigma_status}]")
                for m in matches
            ]
            picked = fzf_pick_choice(
                user_entries,
                prompt="user > ",
                header=f"Found {len(matches)} matching users · pick user",
                height="45%",
            )
            if not picked:
                return
            targets = [m for m in matches if m.email == picked]
    elif sel == "3":
        targets = [r for r in records if r.is_departed and r.sigma_status == "active"]
    else:
        return

    if not targets:
        print("No matching active users found to deactivate.")
        return

    targets.sort(key=lambda r: (r.full_name.casefold(), r.email.casefold()))

    owner = input("\nEnter destination email for reassigned documents (or 'archive' to archive docs): ").strip()
    if not owner:
        print("Cancelled.")
        return

    archive_docs = owner.lower() == "archive"
    new_owner_email = None if archive_docs else owner

    mode = input("Execute live or dry-run? [dry-run/live] (default: dry-run): ").strip().lower()
    is_live = mode in ("live", "execute", "apply", "yes")

    run_remediation_plan(
        client=client,
        targets=targets,
        downgrade=False,
        deactivate=True,
        new_owner_email=new_owner_email,
        archive_docs=archive_docs,
        execute=is_live,
        auto_confirm=True,
        one_by_one=True,
    )


def interactive_build_report(records: list[MemberAuditRecord]) -> None:
    # 1. Account type
    acct_types = sorted({r.member_type for r in records if r.member_type}, key=str.casefold)
    acct_entries = [("ALL", "ALL — All account types")] + [(t, f"{t:<14} account type") for t in acct_types]
    selected_acct = fzf_pick_multi(
        acct_entries,
        prompt="account type > ",
        header="Step 1/3: Select Account Type(s) · TAB multi-select · Ctrl-A all · Enter confirm · Esc cancel",
        height="45%",
    )
    if not selected_acct:
        print("Cancelled.")
        return

    # 2. License tier
    tier_entries = [
        ("ALL", "ALL — All license tiers"),
        ("Build", "Build tier (Authoring, SQL, dashboards, manage-users)"),
        ("Analyze", "Analyze tier (Exploration, AI formulas, exports)"),
        ("Act", "Act tier (Actions, recipient exports)"),
        ("View", "View tier (Read-only viewers)"),
    ]
    selected_tiers = fzf_pick_multi(
        tier_entries,
        prompt="license tier > ",
        header="Step 2/3: Select License Tier(s) · TAB multi-select · Ctrl-A all · Enter confirm · Esc cancel",
        height="40%",
    )
    if not selected_tiers:
        print("Cancelled.")
        return

    # 3. Employee type
    emp_entries = [
        ("ALL", "ALL — All employee / user kinds"),
        ("active", "Active Employee — Currently employed at company (internal)"),
        ("departed", "Departed Employee — Terminated in ADP / Okta"),
        ("guest", "Guest User — External collaborator in Sigma"),
        ("embed", "Embed Account — Automated service / embed user"),
    ]
    selected_emp = fzf_pick_multi(
        emp_entries,
        prompt="employee type > ",
        header="Step 3/3: Select Employee Type(s) · TAB multi-select · Ctrl-A all · Enter confirm · Esc cancel",
        height="40%",
    )
    if not selected_emp:
        print("Cancelled.")
        return

    filtered = records

    if "ALL" not in selected_acct:
        acct_set = {a.lower() for a in selected_acct}
        filtered = [r for r in filtered if r.member_type.lower() in acct_set]

    if "ALL" not in selected_tiers:
        tier_set = {t.lower() for t in selected_tiers}
        filtered = [r for r in filtered if r.license_type.lower() in tier_set]

    if "ALL" not in selected_emp:
        emp_set = set(selected_emp)

        def match_emp(r: MemberAuditRecord) -> bool:
            if "active" in emp_set and not r.is_departed and r.user_kind == "internal":
                return True
            if "departed" in emp_set and r.is_departed:
                return True
            if "guest" in emp_set and r.user_kind == "guest":
                return True
            if "embed" in emp_set and r.user_kind == "embed":
                return True
            return False

        filtered = [r for r in filtered if match_emp(r)]

    acct_label = "ALL" if "ALL" in selected_acct else ", ".join(selected_acct)
    tier_label = "ALL" if "ALL" in selected_tiers else ", ".join(selected_tiers)
    emp_label = "ALL" if "ALL" in selected_emp else ", ".join(selected_emp)

    print("\n" + "=" * 60)
    print("                    CUSTOM BUILT REPORT                    ")
    print("=" * 60)
    print("Filters applied:")
    print(f"  • Account Type:   {acct_label}")
    print(f"  • License Tier:   {tier_label}")
    print(f"  • Employee Type:  {emp_label}")
    print(f"  • Total Matches:  {len(filtered)}")
    print("=" * 60 + "\n")

    print(AuditReporter.render_table(filtered, utilization_mode=True))

    ans = input("\nPress Enter to return to menu (or type 'csv' to export to /tmp/custom_report.csv): ").strip().lower()
    if ans in ("csv", "export"):
        csv_path = Path("/tmp/custom_report.csv")
        csv_path.write_text(AuditReporter.render_csv(filtered), encoding="utf-8")
        print(f"Exported {len(filtered)} records to {csv_path}")


def interactive_menu(
    records: list[MemberAuditRecord],
    client: SigmaClient,
    type_to_tier: dict[str, str] | None = None,
) -> int:
    while True:
        departed = [r for r in records if r.is_departed]
        departed_active_paid = [r for r in departed if r.sigma_status == "active" and r.is_paid]
        paid_active = [r for r in records if r.sigma_status == "active" and r.is_paid]
        dormant_paid = [r for r in paid_active if r.utilization_status == "DORMANT"]
        underutilized_build = [r for r in paid_active if r.utilization_status == "UNDERUTILIZED_BUILD"]

        print("\n" + AuditReporter.render_summary(records))

        menu_entries = [
            ("1", "1. List departed employees with active PAID licenses (CRITICAL)"),
            ("2", "2. Paid license utilization & activity (all active paid seats)"),
            ("3", "3. List dormant paid seats (0 activity in 30 days)"),
            ("4", "4. List view-only Builders (holding Build tier but 0 edits)"),
            ("5", "5. List all departed employees in Sigma"),
            ("6", "6. Downgrade active departed paid users to View (dry-run preview)"),
            ("7", "7. Downgrade dormant paid users to View (dry-run preview or full prod send it)"),
            ("8", "8. Batch deactivate active departed users (dry-run preview)"),
            ("9", "9. Deactivate one-by-one (interactive step-through or pick user)"),
            ("10", "10. Custom search user"),
            ("11", "11. Export audit to CSV /tmp/sigma_departed_audit.csv"),
            ("12", "12. Build a report (custom multi-select: Account, License, & Employee type)"),
            ("13", "13. GTM Team (Teri Hatfield) — direct/indirect license breakdown"),
            ("14", "14. Value Engineering (Katie Barends) — direct/indirect license breakdown"),
            ("q", "q. Quit"),
        ]

        choice = fzf_pick_choice(
            menu_entries,
            prompt="option > ",
            header="Choose an option · ↑/↓ navigate · type to filter · Enter select · Esc quit",
            height="55%",
        )

        if not choice or choice in ("q", "quit", "exit"):
            return 0
        elif choice == "1":
            print(AuditReporter.render_table(departed_active_paid))
            input("\nPress Enter to return to menu...")
        elif choice == "2":
            print(AuditReporter.render_table(paid_active, utilization_mode=True))
            input("\nPress Enter to return to menu...")
        elif choice == "3":
            print(AuditReporter.render_table(dormant_paid, utilization_mode=True))
            input("\nPress Enter to return to menu...")
        elif choice == "4":
            print(AuditReporter.render_table(underutilized_build, utilization_mode=True))
            input("\nPress Enter to return to menu...")
        elif choice == "5":
            print(AuditReporter.render_table(departed))
            input("\nPress Enter to return to menu...")
        elif choice == "6":
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
            input("\nPress Enter to return to menu...")
        elif choice == "7":
            if not dormant_paid:
                print("\nNo dormant paid users found to downgrade.")
                input("\nPress Enter to return to menu...")
                continue

            mode = fzf_pick_choice(
                [
                    ("dry-run", "Dry-run preview (simulate downgrades, no changes made)"),
                    ("prod", "Full prod send it (apply live downgrades to View in Sigma)"),
                ],
                prompt="mode > ",
                header=f"Downgrade {len(dormant_paid)} dormant paid user(s) to View · pick mode",
            )
            if not mode:
                print("Cancelled.")
                continue

            is_prod = (mode == "prod")
            run_remediation_plan(
                client=client,
                targets=dormant_paid,
                downgrade=True,
                deactivate=False,
                new_owner_email=None,
                archive_docs=False,
                execute=is_prod,
                auto_confirm=False,
            )
            input("\nPress Enter to return to menu...")
        elif choice == "8":
            owner = input("Enter destination email for reassigned documents (or 'archive'): ").strip()
            if owner:
                archive_docs = owner.lower() == "archive"
                run_remediation_plan(
                    client=client,
                    targets=departed_active_paid,
                    downgrade=False,
                    deactivate=True,
                    new_owner_email=None if archive_docs else owner,
                    archive_docs=archive_docs,
                    execute=False,
                    auto_confirm=False,
                )
                input("\nPress Enter to return to menu...")
        elif choice == "9":
            interactive_deactivate_one_by_one(records, client)
        elif choice == "10":
            term = input("Search query: ").strip().lower()
            matches = [r for r in records if term in r.email.lower() or term in r.full_name.lower()]
            print(AuditReporter.render_table(matches))
            input("\nPress Enter to return to menu...")
        elif choice == "11":
            csv_path = Path("/tmp/sigma_departed_audit.csv")
            csv_path.write_text(AuditReporter.render_csv(records), encoding="utf-8")
            print(f"Exported {len(records)} records to {csv_path}")
            input("\nPress Enter to return to menu...")
        elif choice == "12":
            interactive_build_report(records)
        elif choice == "13":
            try:
                run_reporting_team_prompt(
                    records, client, GTM_TEAM_LABEL, GTM_MANAGER_EMAIL, type_to_tier
                )
            except Exception as exc:
                print(f"ERROR building GTM Team report: {exc}", file=sys.stderr)
            input("\nPress Enter to return to menu...")
        elif choice == "14":
            try:
                run_reporting_team_prompt(
                    records, client, VE_TEAM_LABEL, VE_MANAGER_EMAIL, type_to_tier
                )
            except Exception as exc:
                print(f"ERROR building Value Engineering report: {exc}", file=sys.stderr)
            input("\nPress Enter to return to menu...")


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

    if args.gtm_team or args.value_engineering:
        records = build_audit_records(
            raw_members,
            [],
            type_to_tier,
            activities=ActivityStore.load_cached(cache_file=args.activity_file),
        )
        try:
            if args.gtm_team:
                run_reporting_team_prompt(
                    records, sigma_client, GTM_TEAM_LABEL, GTM_MANAGER_EMAIL, type_to_tier
                )
            if args.value_engineering:
                run_reporting_team_prompt(
                    records, sigma_client, VE_TEAM_LABEL, VE_MANAGER_EMAIL, type_to_tier
                )
        except Exception as exc:
            print(f"ERROR building team report: {exc}", file=sys.stderr)
            return 1
        return 0

    # Fetch departed employees from Snowflake ADP
    try:
        departed_workers = SnowflakeWorkerStore.fetch_from_snowflake(force_refresh=args.refresh)
    except Exception as exc:
        print(f"ERROR querying Snowflake ADP: {exc}", file=sys.stderr)
        return 1

    # Load activity store
    activities = ActivityStore.load_cached(cache_file=args.activity_file)

    # Build audit records
    records = build_audit_records(raw_members, departed_workers, type_to_tier, activities=activities)

    # Interactive mode check
    if args.interactive or (len(sys.argv) == 1 and sys.stdin.isatty()):
        return interactive_menu(records, sigma_client, type_to_tier)

    # Filtering
    filtered = records
    is_custom_filter = bool(args.account_types or args.license_types or args.employee_types)
    is_util_mode = args.utilization or args.dormant_only or args.underutilized_only or is_custom_filter

    if is_util_mode and not args.all_members and not args.users and not is_custom_filter:
        filtered = [r for r in filtered if r.sigma_status == "active" and r.is_paid]
    elif not args.all_members and not args.users and not is_custom_filter:
        filtered = [r for r in filtered if r.is_departed]

    if args.users:
        user_set = {u.strip().lower() for u in args.users}
        filtered = [r for r in filtered if r.email in user_set]

    if args.account_types:
        acct_set = {a.strip().lower() for a in args.account_types}
        filtered = [r for r in filtered if r.member_type.lower() in acct_set]

    if args.license_types:
        tier_set = {t.strip().lower() for t in args.license_types}
        filtered = [r for r in filtered if r.license_type.lower() in tier_set]

    if args.employee_types:
        emp_set = set(args.employee_types)
        if "all" not in emp_set:

            def match_cli_emp(r: MemberAuditRecord) -> bool:
                if "active" in emp_set and not r.is_departed and r.user_kind == "internal":
                    return True
                if "departed" in emp_set and r.is_departed:
                    return True
                if "guest" in emp_set and r.user_kind == "guest":
                    return True
                if "embed" in emp_set and r.user_kind == "embed":
                    return True
                return False

            filtered = [r for r in filtered if match_cli_emp(r)]

    if args.paid_only:
        filtered = [r for r in filtered if r.is_paid]

    if args.active_only:
        filtered = [r for r in filtered if r.sigma_status == "active"]

    if args.dormant_only:
        filtered = [r for r in filtered if r.utilization_status == "DORMANT"]

    if args.underutilized_only:
        filtered = [r for r in filtered if r.utilization_status == "UNDERUTILIZED_BUILD"]

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
            one_by_one=args.one_by_one,
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
        print(AuditReporter.render_table(filtered, utilization_mode=is_util_mode))

    return 0


if __name__ == "__main__":
    sys.exit(main())
