"""Parent menu groups for interactive `de` (fzf)."""

from __future__ import annotations

from .registry import Tool, tools_sorted

# key, fzf subtitle, guide text (`de groups` and the line above a group's tools).
MENU_GROUPS: tuple[tuple[str, str, str], ...] = (
    (
        "sfdc",
        "Opportunity formulas, CHS → SFDC, SOQL, limits, API users",
        (
            "Salesforce-facing workflows: Opportunity formula MERGE to Snowflake, "
            "customer health score sync to Account, ad-hoc Opp SOQL, org API "
            "limits (max and remaining), and integration user password expiration. Uses SF CLI (and Snowflake browser SSO for writes)."
        ),
    ),
    (
        "sigma",
        "Sigma CLI, licenses, search, sharing, deactivate and reactivate",
        (
            "Sigma Computing admin: API CLI, license inventory, workbook search "
            "and lists, data-model connection swap, one data model or dataset "
            "ownership transfer, dataset migration, workspace and folder bootstrap, "
            "per-folder sharing, user downgrade, deactivate, and reactivate, "
            "creating a team or adding people to one from Sam Allen's directs, changing licenses "
            "for one team, and moving Act (Build license) users who are not building to Act+. "
            "OAuth via Sigma CLI."
        ),
    ),
    (
        "airflow",
        "Astro deployments, DAG deploy, DAG control, user access",
        (
            "Astronomer / Airflow: list deployments, deploy DAGs to Astro, "
            "the interactive DAG control center (trigger runs, watch logs), "
            "and workspace or deployment user access."
        ),
    ),
    (
        "bi_apps",
        "Daily Pulse and other bi-applications monorepo tools",
        (
            "skillman-iterable/bi_applications: DE Daily Pulse (dry-run or smoke post "
            "to #daily_pulse_est). Uses scripts/run-local.sh and repo .env layers. "
            "Gcloud Auth for Territory Planner signs in and prints the UAT Cloud Run revision."
        ),
    ),
    (
        "local",
        "Local bi-astronomer runners (laptop)",
        (
            "Local bi-astronomer scripts (not scheduled in Astro): AirOps, order sync, "
            "and Outlook SNDS. git-repo-check fetches clones under ~/github and "
            "prints how far each branch is ahead or behind its base."
        ),
    ),
    (
        "deploy",
        "SALT & Netlify production app deploys",
        (
            "Production app deploys: SALT GCP Firebase preview and Netlify prod for "
            "salt_app. Always confirm the target environment before running."
        ),
    ),
    (
        "dbt",
        "dbt Core / Cloud in configured projects",
        (
            "Run dbt Core or Cloud against projects in ~/.config/de/config.toml "
            "(-p project, --pick, or forward any dbt args)."
        ),
    ),
    (
        "fivetran",
        "Model runs, transformation runs, and what is running now",
        (
            "Fivetran individual model runs, a picker to run any transformation, "
            "and a list of transformations and connectors that are running."
        ),
    ),
    (
        "personal",
        "Court Reserve and other personal jobs",
        (
            "Personal machine jobs. Court Reserve status check reads the weekend "
            "scheduler, booking script, defaults, last run, sleep, and live processes. "
            "Court Reserve Normal Schedule runs the live 7:45 AM booking. "
            "Court Reserve Next Launch prints when that job fires next. "
            "Court Reserve Live Status shows the scheduler, any booking process, and the last log lines."
        ),
    ),
)

_CATEGORY_TO_GROUP: dict[str, str] = {
    "airflow": "airflow",
    "local": "local",
    "ops": "local",
    "fivetran": "fivetran",
    "dbt": "dbt",
    "salesforce": "sfdc",
    "sigma": "sigma",
    "deploy": "deploy",
    "bi_apps": "bi_apps",
    "personal": "personal",
    "general": "local",
}


def menu_group_for_tool(tool: Tool) -> str:
    return _CATEGORY_TO_GROUP.get(tool.category, "local")


def group_blurb(key: str) -> str | None:
    for group_key, _subtitle, blurb in MENU_GROUPS:
        if group_key == key:
            return blurb
    return None


def menu_group_label(key: str) -> str:
    from .menu_state import ALL_TOOLS_KEY, GUIDE_KEY, PINNED_KEY, RECENT_KEY

    labels = {
        "airflow": "Astronomer",
        "local": "Local",
        "deploy": "Deploy",
        "fivetran": "Fivetran",
        "dbt": "dbt",
        "sfdc": "SFDC",
        "sigma": "Sigma",
        "bi_apps": "BI Apps",
        "personal": "Personal",
        ALL_TOOLS_KEY: "All tools",
        RECENT_KEY: "Recent",
        PINNED_KEY: "Pinned",
        GUIDE_KEY: "Guide",
    }
    return labels.get(key, key)


def breadcrumb(group_key: str | None, tool_name: str | None = None) -> str:
    parts: list[str] = []
    if group_key:
        parts.append(menu_group_label(group_key))
    if tool_name:
        parts.append(tool_name)
    return " › ".join(parts)


def tools_in_menu_group(group_key: str) -> tuple[Tool, ...]:
    return tuple(
        t for t in tools_sorted() if menu_group_for_tool(t) == group_key
    )


def ordered_menu_groups() -> list[tuple[str, str]]:
    """Platforms in the interactive menu, alphabetized by display label."""
    return sorted(
        ((key, subtitle) for key, subtitle, _blurb in MENU_GROUPS),
        key=lambda pair: menu_group_label(pair[0]).casefold(),
    )


_MENU_DESC_MAX = 76


def tools_by_names(names: list[str]) -> tuple[Tool, ...]:
    from .registry import find_tool

    out: list[Tool] = []
    seen: set[str] = set()
    for name in names:
        tool = find_tool(name)
        if tool is None or tool.name in seen:
            continue
        seen.add(tool.name)
        out.append(tool)
    return tuple(out)


def tool_menu_description(tool: Tool) -> str:
    """Short line for fzf (full ``description`` stays on ``de --help`` / Typer)."""
    if tool.menu_summary:
        return tool.menu_summary
    text = " ".join(tool.description.split())
    if len(text) <= _MENU_DESC_MAX:
        return text
    return text[: _MENU_DESC_MAX - 1].rstrip() + "…"


def tool_menu_line(
    tool: Tool,
    *,
    include_hint: bool = False,
    hint_max: int = 52,
) -> str:
    line = tool_menu_description(tool)
    if include_hint and tool.menu_hint:
        hint = tool.menu_hint
        if len(hint) > hint_max:
            hint = hint[: hint_max - 1].rstrip() + "…"
        line = f"{line}  —  {hint}"
    return line
