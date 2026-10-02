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
    # Shown after picking a tool in the interactive menu (runtime expectations).
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
        menu_summary="Pick a DAG, trigger runs, watch task logs (Astro)",
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
        menu_hint=(
            "Organization remove is UI-only (opens Astronomer). "
            "Workspace remove only drops workspace access."
        ),
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
        name="customer-health-score",
        description=(
            "Snowflake customer_health_score → SFDC Account.Customer_Health_Score__c "
            "(sandbox UAT / capped prod). Dry-run unless --live-write; "
            "--prod for production (prompts env if omitted)."
        ),
        menu_summary="CHS Snowflake → SF Account health score (sandbox / capped prod)",
        menu_hint="Dry-run default · --live-write + --prod for capped prod SF updates",
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
        menu_summary="Fivetran individual model run (interactive dbt selector)",
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
        menu_summary="Run transformations",
        menu_hint="Enter runs the job. Right arrow shows the latest run log tail.",
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
        menu_summary="Running transformations and connectors",
        menu_hint="Right arrow shows the log. Nothing is started.",
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
            "SF Opportunity formula extract → Snowflake MERGE "
            "(UDI_PROD.UDI_STG.OPPORTUNITY_FORMULA_VALUES). "
            "No args = full run + Snowflake browser SSO + MERGE."
        ),
        menu_summary="Opp formula fields → UDI_PROD MERGE (menu = full + Snowflake SSO)",
        menu_hint=(
            "Full prod extract (~40k Opps) · Okta for Snowflake · MERGE to UDI_PROD (~2 min)"
        ),
        menu_requires_confirm=True,
        script="scripts/run-opp-formula-refresh-local.sh",
        root="bi_astronomer",
        aliases=("opp-formulas", "opp-formula", "sfdc-formulas"),
        category="local",
        default_args=(
            "--full",
            "--write-snowflake",
            "--snowflake-auth",
            "externalbrowser",
        ),
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
        menu_hint="Fetches each repo. Pass a path to scan somewhere else.",
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
        menu_summary="Mint Sigma token, then Sigma CLI (or forward args)",
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
        menu_summary="Bulk migrate Sigma datasets → data models (dry-run default)",
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
        menu_summary="Downgrade Sigma user + migrate My Documents (dry-run)",
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
        menu_summary="Deactivate Sigma user(s); reassign docs (dry-run)",
        menu_hint="Dry-run default · --execute --yes applies Sigma member archive",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_user_deactivate.py"),
        ),
        category="sigma",
        aliases=("sigma-user-deactivate", "deactivate-sigma-user"),
    ),
    Tool(
        name="sigma-licenses",
        description=(
            "Sigma license inventory — summary, report, teams, deactivate "
            "(no args → fzf menu; OAuth via Sigma CLI)"
        ),
        menu_summary="Sigma license inventory, teams, deactivate (fzf menu)",
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
        menu_summary="List Sigma workbooks as {id, name}",
        script=str(Path.home() / "de" / "sigma_workbooks_list.sh"),
        category="sigma",
        aliases=("sigma-wb-list",),
    ),
    Tool(
        name="sigma-bi-dashboards",
        description=(
            "BI-owned Sigma workbooks as JSON/CSV: team, dashboard URL, endorsed, "
            "owner email, last viewed (optional audit JSON / snow)."
        ),
        menu_summary="BI Sigma workbook inventory (JSON/CSV, last viewed)",
        argv=(
            "python3",
            str(Path.home() / "de" / "scripts" / "sigma_bi_dashboard_inventory.py"),
        ),
        category="sigma",
        aliases=("sigma-bi-inventory", "bi-dashboards"),
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
