"""Tool registry — add new use cases here.

Each tool is either:
  • argv  — call an existing CLI, e.g. ("astro", "deploy")
  • script — run a file under a configured repo root
              (root key → ~/.config/de/config.toml [paths])

Exactly one of argv / script must be set.

Interactive `de` / `de --list` display tools in alphabetical order by name.
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
    # CLI form: subprocess argv (first element is the executable).
    argv: tuple[str, ...] | None = None
    # Script form: path relative to a configured root (or absolute).
    script: str | None = None
    root: str | None = None  # key in config [paths], required when script is relative
    cwd_root: str | None = None  # optional: chdir to this path key before running

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
        script="scripts/run-airops-local.sh",
        root="bi_astronomer",
        aliases=("airops",),
        category="local",
    ),
    Tool(
        name="astro-dags",
        description="Astro DAG control center — pick, trigger, and watch DAG runs",
        script="scripts/astro_dags.sh",
        root="bi_astronomer",
        aliases=("astro", "dags"),
        category="airflow",
    ),
    Tool(
        name="astro-deploy",
        description="Run `astro deploy` from the bi-astronomer repo",
        argv=("astro", "deploy"),
        cwd_root="bi_astronomer",
        aliases=("deploy",),
        category="airflow",
    ),
    Tool(
        name="astro-ls",
        description="List Astro deployments (`astro deployment list`)",
        argv=("astro", "deployment", "list"),
        category="airflow",
    ),
    Tool(
        name="dbt",
        description=(
            "Run dbt (Core or Cloud CLI) in a configured local project "
            "(-p NAME / --pick / projects; args forwarded)"
        ),
        argv=("dbt",),
        # cwd + binary resolved per-invocation from [dbt.projects]
        category="dbt",
    ),
    Tool(
        name="fivetran",
        description="Fivetran Individual Model Run — interactive dbt selector",
        script="scripts/fivetran_individual_model_run",
        root="bi_astronomer",
        aliases=("ft", "fivetran-model"),
        category="fivetran",
    ),
    Tool(
        name="netlify-prod-deploy",
        description="Build + Netlify prod deploy for salt_app (cd salt_app && netlify_prod_deploy)",
        script="scripts/run-netlify-prod-deploy.sh",
        root="bi_astronomer",
        aliases=("netlify-prod", "netlify"),
        category="sigma",
    ),
    Tool(
        name="opp-formula-refresh",
        description=(
            "SF Opportunity formula extract → optional Snowflake MERGE "
            "(UDI_PROD.UDI_STG.OPPORTUNITY_FORMULA_VALUES)"
        ),
        script="scripts/run-opp-formula-refresh-local.sh",
        root="bi_astronomer",
        aliases=("opp-formulas", "opp-formula", "sfdc-formulas"),
        category="local",
    ),
    Tool(
        name="order-sync-local",
        description="Run order sync locally",
        script="scripts/run-order-sync-local.sh",
        root="bi_astronomer",
        aliases=("order-sync",),
        category="local",
    ),
    Tool(
        name="outlook-snds-local",
        description="Run Outlook SNDS ingestion locally",
        script="scripts/run-outlook-snds-local.sh",
        root="bi_astronomer",
        aliases=("outlook-snds", "snds"),
        category="local",
    ),
    Tool(
        name="salt-deploy",
        description="Build/deploy salt_app_gcp Firebase preview (deploy_salt_gcp)",
        argv=(str(Path.home() / "bin" / "deploy_salt_gcp"),),
        cwd_root="bi_react",
        aliases=("salt",),
        category="sigma",
    ),
    Tool(
        name="sf-opp-query",
        description="SF CLI Opportunity formula SOQL (human / csv / json; optional --pin)",
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
