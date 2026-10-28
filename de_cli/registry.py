"""Tool registry — add new use cases here.

Each tool is either:
  • argv  — call an existing CLI, e.g. ("astro", "deploy")
  • script — run a file under a configured repo root
              (root key → ~/.config/de/config.toml [paths])

Exactly one of argv / script must be set.

Interactive `de` picks a parent group (SFDC, Sigma, Airflow, Local, Deploy, …)
then tools in that group (alpha). `de groups` explains buckets; `de --list` is flat.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import load_config


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    aliases: tuple[str, ...] = ()
    category: str = "general"
    # Short label for fzf menu (~60–80 chars); ``description`` is full help text.
    menu_summary: str = ""
    # Shown on the confirm prompt. Set only when menu_requires_confirm is true.
    menu_hint: str = ""
    # Interactive menu: require typing the tool name before run (deploy / prod writes).
    menu_requires_confirm: bool = False
    # CLI form: subprocess argv (first element is the executable).
    argv: tuple[str, ...] | None = None
    # Script form: path relative to a configured root (or absolute).
    script: str | None = None
    root: str | None = None  # key in config [paths], required when script is relative
    cwd_root: str | None = None  # optional: chdir to this path key before running
    # Applied only when the tool is invoked with no args (menu pick, bare `de <tool>`).
    default_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        has_argv = bool(self.argv)
        has_script = bool(self.script)
        if has_argv == has_script:
            raise ValueError(
                f"Tool '{self.name}' must set exactly one of argv= or script="
            )
        if self.script and not self.script.startswith("/") and not self.root:
            raise ValueError(
                f"Tool '{self.name}' has a relative script but no root= path key"
            )

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def resolve_command(self) -> list[str]:
        """Return argv ready for exec/subprocess."""
        if self.argv:
            return list(self.argv)

        assert self.script is not None
        script_path = Path(self.script).expanduser()
        if not script_path.is_absolute():
            root = load_config().require_path(self.root or "")
            script_path = root / self.script
        return [str(script_path)]

    def resolve_cwd(self) -> Path | None:
        if not self.cwd_root:
            return None
        return load_config().require_path(self.cwd_root)

    def resolve_display_target(self) -> str:
        if self.argv:
            return " ".join(self.argv)
        return " ".join(self.resolve_command())


# ─── Register tools here ─────────────────────────────────────────────
# Add a Tool(...) row, then `de --list` picks it up. No app.py edits needed.
# Display order is alphabetical by name (see tools_sorted()).
# ─────────────────────────────────────────────────────────────────────
TOOLS: tuple[Tool, ...] = (
    Tool(
        name="airops-local",
        description="Run AirOps → Snowflake ingestion locally",
        menu_summary="AirOps → Snowflake ingestion (local bi-astronomer script)",
        script="scripts/run-airops-local.sh",
        root="bi_astronomer",
        aliases=("airops",),
        category="local",
    ),
    Tool(
        name="astro-dags",
        description="Astro DAG control center — pick, trigger, and watch DAG runs",
        menu_summary="Pick/trigger DAGs; --runs browses success/failed/running logs",
        script="scripts/astro_dags.sh",
        root="bi_astronomer",
        aliases=("astro", "dags"),
        category="airflow",
    ),
    Tool(
        name="astro-deploy",
        description="Run `astro deploy` from the bi-astronomer repo",
        menu_summary="Deploy bi-astronomer DAGs to Astro (`astro deploy`)",
        menu_hint="Deploys to Astro — confirm deployment target before running",
        menu_requires_confirm=True,
        argv=("astro", "deploy"),
        cwd_root="bi_astronomer",
        aliases=("deploy",),
        category="airflow",
    ),
    Tool(
        name="astro-users",
        description=(
            "Provision or deprovision Astronomer users "
            "(workspace or deployment; list / add / update / remove). "
            "No args → fzf. Remove and update pick a user; Enter applies."
        ),
        menu_summary="Add, update, or remove Astro workspace and deployment users",
        script=str(Path.home() / "de" / "astro_users.sh"),
        aliases=("astro-user", "astro-access"),
        category="airflow",
    ),
    Tool(
        name="astro-ls",
        description="List Astro deployments (`astro deployment list`)",
        menu_summary="List Astro deployments",
        argv=("astro", "deployment", "list"),
        category="airflow",
    ),
    Tool(
        name="daily-pulse",
        description=(
            "DE Daily Pulse (Jira/Airflow/Snowflake/GitHub digest). "
            "Always --dry-run; no args = --dry-run --hours 24."
        ),
        script="scripts/run-local.sh",
        root="bi_applications",
        aliases=("pulse", "de-pulse"),
        category="bi_apps",
        default_args=("--dry-run", "--hours", "24"),
        menu_summary="DE digest: Jira, Airflow, Snowflake, GitHub (dry-run, 24h)",
    ),
    Tool(
        name="daily-pulse-smoke-est",
        description=(
            "DE Daily Pulse smoke: collect + post to #daily_pulse_est "
            "(config/de.personal.yaml when present). No args = --hours 24."
        ),
        menu_summary="Post DE digest to #daily_pulse_est (smoke, 24h)",
        menu_hint="Needs SLACK_BOT_TOKEN in bi-applications .env",
        script="scripts/run-local.sh",
        root="bi_applications",
        aliases=("pulse-smoke",),
        category="bi_apps",
        default_args=("--smoke-est", "--hours", "24"),
        menu_requires_confirm=True,
    ),
    Tool(
        name="gcloud-auth-territory-planner",
        description=(
            "Sign in to gcloud as shawn.skillman@iterable.com, select project "
            "udi-uat-504519, and print the live Territory Planner Cloud Run revision. "
            "Login opens a browser."
        ),
        menu_summary="Gcloud Auth for Territory Planner",
        script=str(Path.home() / "de" / "scripts" / "gcloud_auth_territory_planner.sh"),
        category="bi_apps",
        aliases=("territory-planner-auth", "gcloud-territory"),
    ),
    Tool(
        name="customer-health-score",
        description=(
            "Snowflake customer_health_score → SFDC Account.Customer_Health_Score__c "
            "(sandbox UAT / capped prod). Dry-run unless --live-write; "
            "--prod for production (prompts env if omitted)."
        ),
        menu_summary="CHS Snowflake → SF Account health score (sandbox / capped prod)",
        script="scripts/run-customer-health-score-sandbox.sh",
        root="bi_astronomer",
        aliases=("chs", "account-health", "health-score"),
        category="salesforce",
    ),
    Tool(
        name="dbt",
        description=(
            "Run dbt (Core or Cloud CLI) in a configured local project "
            "(-p NAME / --pick / projects; args forwarded)"
        ),
        menu_summary="dbt in a configured project (-p / --pick; args forwarded)",
        argv=("dbt",),
        # cwd + binary resolved per-invocation from [dbt.projects]
        category="dbt",
    ),
    Tool(
        name="fivetran",
        description="Fivetran Individual Model Run — interactive dbt selector",
        menu_summary="Run a dbt selector",
        script="scripts/fivetran_individual_model_run",
        root="bi_astronomer",
        aliases=("ft", "fivetran-model"),
        category="fivetran",
    ),
    Tool(
        name="fivetran-transformations",
        description=(
            "Run a Fivetran transformation. No args opens an fzf of dbt Core "
            "and Quickstart jobs; Enter runs the one you pick."
        ),
        menu_summary="Run a saved transformation",
        script=str(Path.home() / "de" / "fivetran_transformations.sh"),
        aliases=("ft-run", "transformations"),
        category="fivetran",
    ),
    Tool(
        name="fivetran-running",
        description=(
            "List Fivetran transformations and connectors that are running. "
            "Right arrow shows the platform log for the highlighted job."
        ),
        menu_summary="Show what is running",
        script=str(Path.home() / "de" / "fivetran_running.sh"),
        aliases=("ft-running", "fivetran-live"),
        category="fivetran",
    ),
    Tool(
        name="netlify-prod-deploy",
        description="Build + Netlify prod deploy for salt_app (cd salt_app && netlify_prod_deploy)",
        menu_summary="Netlify prod deploy for salt_app",
        menu_hint="Production Netlify deploy — confirm target site before running",
        menu_requires_confirm=True,
        script="scripts/run-netlify-prod-deploy.sh",
        root="bi_astronomer",
        aliases=("netlify-prod", "netlify"),
        category="deploy",
    ),
    Tool(
        name="opp-formula-refresh",
        description=(
            "SF Opportunity formula extract → Snowflake MERGE. "
            "No args → fzf load profile (open Opps or full)."
        ),
        menu_summary="Opp formulas → UDI_PROD (fzf: open vs full vs smoke)",
        menu_hint="Pick load mode: open-only is faster; full ~40k×80 cols ~7 min",
        menu_requires_confirm=False,
        script=str(Path.home() / "de" / "scripts" / "opp_formula_refresh_menu.py"),
        aliases=("opp-formulas", "opp-formula", "sfdc-formulas"),
        category="salesforce",
    ),
    Tool(
        name="order-sync-local",
        description="Run order sync locally",
        menu_summary="Order sync local runner",
        script="scripts/run-order-sync-local.sh",
        root="bi_astronomer",
        aliases=("order-sync",),
        category="local",
    ),
    Tool(
        name="outlook-snds-local",
        description="Run Outlook SNDS ingestion locally",
        menu_summary="Outlook SNDS ingestion (local)",
        script="scripts/run-outlook-snds-local.sh",
        root="bi_astronomer",
        aliases=("outlook-snds", "snds"),
        category="local",
    ),
    Tool(
        name="git-repo-check",
        description=(
            "Scan local git clones (default ~/github) and print each branch "
            "ahead/behind its base. Fetches before comparing."
        ),
        menu_summary="Scan ~/github clones: branch ahead/behind",
        script=str(Path.home() / "bin" / "git_repo_check.sh"),
        aliases=("git-repos", "repo-check", "repos"),
        category="local",
    ),
    Tool(
        name="salt-deploy",
        description="Build/deploy salt_app_gcp Firebase preview (deploy_salt_gcp)",
        menu_summary="SALT GCP Firebase preview deploy",
        menu_hint="Builds and deploys SALT plugin to Firebase preview",
        menu_requires_confirm=True,
        argv=(str(Path.home() / "bin" / "deploy_salt_gcp"),),
        cwd_root="bi_react",
        aliases=("salt",),
        category="deploy",
    ),
    Tool(
        name="sigma",
        description=(
            "Mint a Sigma API token, then run the Sigma CLI "
            "(no args → interactive sigma> prompt; else forward args)"
        ),
        menu_summary="Mint a token, then open the CLI",
        script=str(Path.home() / "de" / "sigma_cli.sh"),
        category="sigma",
        aliases=("sigma-cli",),
    ),
    Tool(
        name="sigma-dataset-migrate",
        description=(
            "Bulk migrate Sigma datasets to data models "
            "(dry-run by default; --execute to migrate)"
        ),
        menu_summary="Bulk migrate datasets to data models",
        script=str(
            Path.home() / "de" / "scripts" / "migrate_sigma_datasets.sh"
        ),
        category="sigma",
        aliases=("sigma-migrate-datasets", "sigma-datasets-migrate"),
    ),
    Tool(
        name="sigma-user-downgrade",
        description=(
            "Downgrade a Sigma user's account type and migrate their My Documents "
            "contents to another user (dry-run by default)"
        ),
        menu_summary="Downgrade a user and migrate My Documents",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_user_downgrade_handoff.py"),
        ),
        category="sigma",
        aliases=("sigma-downgrade-user", "sigma-user-handoff"),
    ),
    Tool(
        name="sigma-deactivate",
        description=(
            "Deactivate Sigma user(s) and reassign documents to another user "
            "(dry-run by default; same Sigma CLI flow as sigma-user-downgrade)"
        ),
        menu_summary="Deactivate users and reassign their docs",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_user_deactivate.py"),
        ),
        category="sigma",
        aliases=("sigma-user-deactivate", "deactivate-sigma-user"),
    ),
    Tool(
        name="sigma-reactivate-user",
        description=(
            "Reactivate one Sigma user by email. Clears isArchived. "
            "SCIM-inactive users stay inactive unless you pass --force."
        ),
        menu_summary="Reactivate a user by email",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_reactivate_user.py"),
        ),
        category="sigma",
        aliases=("sigma-reactivate", "reactivate-sigma-user"),
    ),
    Tool(
        name="sigma-license-audit",
        description=(
            "Audit Sigma licenses vs Snowflake ADP employee terminations: "
            "identify departed staff holding paid seats, report GTM Team "
            "(Teri Hatfield), reclaim licenses, downgrade or deactivate"
        ),
        menu_summary="License audit, GTM team report, and seat reclamation",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_license_audit.py"),
        ),
        category="sigma",
        aliases=("sigma-audit", "sigma-offboard", "departed-licenses"),
    ),
    Tool(
        name="sigma-gtm-team",
        description=(
            "GTM Team (Teri Hatfield): list all active direct and indirect "
            "reports from ADP with their Sigma license tier. After the list, "
            "ask whether people with no Sigma account should be created as View, "
            "then whether View members should be upgraded to Act+. Sub teams "
            "can be upgraded entirely or one person at a time."
        ),
        menu_summary="GTM Team (Teri Hatfield)",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_license_audit.py"),
            "--gtm-team",
        ),
        category="sigma",
        aliases=("gtm-team", "teri-hatfield-team"),
    ),
    Tool(
        name="sigma-value-engineering",
        description=(
            "Value Engineering (Katie Barends): list all active direct and "
            "indirect reports from ADP with their Sigma license tier. After "
            "the list, ask whether people with no Sigma account should be "
            "created as View, then whether View members should be upgraded to Act+. "
            "Sub teams can be upgraded entirely or one person at a time."
        ),
        menu_summary="Value Engineering (Katie Barends)",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_license_audit.py"),
            "--value-engineering",
        ),
        category="sigma",
        aliases=("value-engineering", "ve-team", "katie-barends-team"),
    ),
    Tool(
        name="sigma-create-team",
        description=(
            "Create a Sigma team and its folder, or add people to a team that "
            "already exists. People come from Sam Allen's direct reports. "
            "Selecting a leader asks whether to choose people one level down or "
            "add that leader and the whole reporting tree."
        ),
        menu_summary="Create team",
        menu_hint="Create a new team, or add people to one that already exists",
        menu_requires_confirm=True,
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_team_admin.py"),
        ),
        default_args=("create",),
        category="sigma",
        aliases=("sigma-new-team",),
    ),
    Tool(
        name="sigma-team-licenses",
        description=(
            "Pick a Sigma team, then change one license type to another. "
            "Members on any other license, and people not on the team, are left unchanged."
        ),
        menu_summary="Team license management",
        menu_hint="Changes only members of one team who currently have the From license",
        menu_requires_confirm=True,
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_team_admin.py"),
        ),
        default_args=("licenses",),
        category="sigma",
        aliases=("team-licenses", "sigma-team-license"),
    ),
    Tool(
        name="sigma-update-license",
        description=(
            "Search Sigma people by name, select one or more with Tab, then "
            "move them to another account type. The billed license is shown "
            "in parentheses next to each account type."
        ),
        menu_summary="Update license",
        menu_hint="Search, Tab to select people, then pick the account type (license)",
        menu_requires_confirm=True,
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_update_license.py"),
        ),
        category="sigma",
        aliases=("update-license", "change-license"),
    ),
    Tool(
        name="sigma-act-build-to-act-plus",
        description=(
            "Report people on the Act account type. That type is a Build license. "
            "The report lists those who are not doing build work, then lets you "
            "choose who to move to Act+ (the true Act license)."
        ),
        menu_summary="Act Account Type Build License move to Act+ (True Act)",
        menu_hint="Moves selected Act (Build) users with no build edits to Act+",
        menu_requires_confirm=True,
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_act_build_to_act_plus.py"),
        ),
        category="sigma",
        aliases=("act-to-act-plus", "act-build-to-act-plus"),
    ),
    Tool(
        name="sigma-licenses",
        description=(
            "Sigma license inventory — summary, report, teams, deactivate "
            "(no args → fzf menu; OAuth via Sigma CLI)"
        ),
        menu_summary="License inventory, teams, and deactivate",
        script="scripts/sigma-licenses/sigma-licenses",
        root="bi_astronomer",
        cwd_root="bi_astronomer",
        aliases=("sigma-lic", "licenses"),
        category="sigma",
    ),
    Tool(
        name="sigma-workbooks-list",
        description=(
            "List Sigma workbooks (auto-re-mint token on 401 / invalid token), "
            "prints entries[] as {id, name}."
        ),
        menu_summary="List workbooks",
        script=str(Path.home() / "de" / "sigma_workbooks_list.sh"),
        category="sigma",
        aliases=("sigma-wb-list",),
    ),
    Tool(
        name="sigma-ensure-workspace",
        description=(
            "Create the Iterable | Data & Analytics Sigma workspace and its folders "
            "when they are missing. Existing workspaces, folders, and documents "
            "are left unchanged."
        ),
        menu_summary="Create Data & Analytics workspace folders if missing",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_ensure_workspace.py"),
        ),
        category="sigma",
        aliases=("sigma-workspace", "ensure-sigma-workspace"),
    ),
    Tool(
        name="sigma-share-workspace",
        description=(
            "Walk Iterable | Data & Analytics and confirm who to share each "
            "workspace and folder with. Default grantee is all members of the "
            "organization. fzf can pick a team or people instead. Nothing is "
            "shared until you confirm that node."
        ),
        menu_summary="Confirm sharing for each Data & Analytics folder",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_share_workspace.py"),
        ),
        category="sigma",
        aliases=("sigma-share", "share-sigma-workspace"),
    ),
    Tool(
        name="sigma-search-workbooks",
        description=(
            "Search Sigma workbooks by name. "
            "* and ? are wildcards. Plain text matches anywhere in the name. "
            "No args prompts for the pattern."
        ),
        menu_summary="Search workbooks by name (* and ? wildcards)",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_search_workbooks.py"),
        ),
        category="sigma",
        aliases=("sigma-find-workbook", "find-workbook"),
    ),
    Tool(
        name="sigma-swap-data-model-connection",
        description=(
            "Swap a data model's connection, or transfer ownership of one "
            "data model or dataset. No args opens fzf: type to filter, "
            "Up/Down wraps. Preview unless you confirm or pass --execute --yes."
        ),
        menu_summary="Swap a model connection, or transfer one model/dataset",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_swap_data_model_connection.py"),
        ),
        category="sigma",
        aliases=("sigma-swap-connection", "sigma-reassign-connection"),
    ),
    Tool(
        name="sigma-move-workbooks",
        description=(
            "Move selected Sigma workbooks between folders/workspaces. "
            "No args → fzf browse source/dest folders, pick workbooks, owner, confirm."
        ),
        menu_summary="URL or fzf folders; pick workbooks; destination; confirm move",
        script=str(Path.home() / "de" / "scripts" / "sigma_move_workbooks.py"),
        category="sigma",
        aliases=("sigma-move-wb", "move-sigma-workbooks"),
    ),
    Tool(
        name="sigma-reassign-workbook",
        description=(
            "Transfer ownership of exactly one Sigma workbook. "
            "Paste its URL or inode ID, pick the new owner, review, and confirm."
        ),
        menu_summary="Transfer one workbook to a new owner",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_reassign_workbook.py"),
        ),
        category="sigma",
        aliases=("sigma-transfer-workbook", "reassign-workbook"),
    ),
    Tool(
        name="sigma-bi-dashboards",
        description=(
            "BI-owned Sigma workbooks as JSON/CSV: team, dashboard URL, endorsed, "
            "owner email, last viewed (optional audit JSON / snow)."
        ),
        menu_summary="Workbook inventory",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_bi_dashboard_inventory.py"),
        ),
        category="sigma",
        aliases=("sigma-bi-inventory", "bi-dashboards"),
    ),
    Tool(
        name="slack-faq-import",
        description=(
            "Import Slack workspace export zip/folder into messages.csv "
            "(no API token; use after admin export)"
        ),
        menu_summary="Import Slack export zip → messages.csv for FAQ report",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "slack_export_import.py"),
        ),
        category="local",
        aliases=("slack-export-import", "slack-faq-ingest"),
    ),
    Tool(
        name="slack-faq-sync",
        description=(
            "Incremental Slack channel export for FAQ mining "
            "(conversations.history + replies; default channel C01V64XA4AK)"
        ),
        menu_summary="Sync Slack channel messages to local CSV",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "slack_channel_sync.py"),
        ),
        category="local",
        aliases=("slack-sync", "slack-faq-export"),
    ),
    Tool(
        name="slack-faq-report",
        description="FAQ frequency CSVs from synced Slack messages (faq_report, by week)",
        menu_summary="Build FAQ report from local Slack export",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "slack_faq_report.py"),
        ),
        category="local",
        aliases=("slack-faq", "slack-questions"),
    ),
    Tool(
        name="slack-faq-daily",
        description="Daily Slack FAQ job: sync channel then regenerate FAQ CSVs",
        menu_summary="Daily Slack sync + FAQ report",
        script=str(Path.home() / "de" / "scripts" / "slack_faq_daily.sh"),
        category="local",
        aliases=("slack-faq-pipeline",),
    ),
    Tool(
        name="sfdc-limits",
        description=(
            "Salesforce REST org limits: maximum and remaining allocations "
            "(services/data/v{apiVersion}/limits). Uses the SF CLI org from "
            "opp-formula-refresh (default iterable-prod)."
        ),
        menu_summary="Org API limits (max and remaining)",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sfdc_limits.py"),
        ),
        category="salesforce",
        aliases=("sf-limits", "org-limits"),
    ),
    Tool(
        name="sfdc-api-users",
        description=(
            "Active Salesforce integration and API-only users, with password "
            "expiration. Expired passwords are repeated in their own table. "
            "Uses the SF CLI org from opp-formula-refresh (default iterable-prod)."
        ),
        menu_summary="API users and password expiration",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sfdc_api_users.py"),
        ),
        category="salesforce",
        aliases=("sf-api-users", "api-users"),
    ),
    Tool(
        name="court-reserve-status",
        description=(
            "Read-only CourtReserve weekend readiness: LaunchAgent schedule, "
            "booking script, reservation defaults, last log lines, Mac sleep, "
            "and any booking process still running."
        ),
        menu_summary="Court Reserve Status Check",
        script=str(Path.home() / "de" / "scripts" / "court_reserve_status.sh"),
        category="personal",
        aliases=("courtreserve", "cr-status"),
    ),
    Tool(
        name="court-reserve-schedule",
        description=(
            "Run the normal CourtReserve booking: live, fast, submit at 7:45:00 AM. "
            "Same script as ~/bin/run_booking.sh."
        ),
        menu_summary="Court Reserve Normal Schedule",
        menu_hint="Live booking — waits, then clicks Save at 7:45 AM",
        menu_requires_confirm=True,
        script=str(Path.home() / "bin" / "run_booking.sh"),
        category="personal",
        aliases=("cr-schedule", "court-reserve-book"),
    ),
    Tool(
        name="court-reserve-next",
        description=(
            "Read-only next CourtReserve LaunchAgent run: when it fires, "
            "how long until then, and the booking date (the day after launch)."
        ),
        menu_summary="Court Reserve Next Launch",
        script=str(Path.home() / "de" / "scripts" / "court_reserve_next.py"),
        category="personal",
        aliases=("cr-next",),
    ),
    Tool(
        name="court-reserve-live",
        description=(
            "Read-only CourtReserve live status: scheduler state, any booking "
            "process still running, and the last five log lines."
        ),
        menu_summary="Court Reserve Live Status",
        script=str(Path.home() / "de" / "scripts" / "court_reserve_live.sh"),
        category="personal",
        aliases=("cr-live",),
    ),
    Tool(
        name="sf-opp-query",
        description="SF CLI Opportunity formula SOQL (human / csv / json; optional --pin)",
        menu_summary="Opportunity formula SOQL via SF CLI (human / csv / json)",
        script="scripts/sf_opp_query.sh",
        root="bi_astronomer",
        aliases=("sf-query", "sfq", "sfdc-query"),
        category="salesforce",
    ),
)


def tools_sorted() -> tuple[Tool, ...]:
    """Tools in alphabetical order by primary name (case-insensitive)."""
    return tuple(sorted(TOOLS, key=lambda t: t.name.lower()))


def find_tool(name: str) -> Tool | None:
    key = name.lower().strip()
    for tool in TOOLS:
        if key in {n.lower() for n in tool.names}:
            return tool
    return None
