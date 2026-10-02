"""Parent menu groups for interactive `de` (fzf)."""

from __future__ import annotations

from .registry import Tool, tools_sorted

# (key, subtitle for fzf row) — default order by typical use; last-used group moves up.
MENU_GROUPS: tuple[tuple[str, str], ...] = (
    ("sfdc", "Opportunity formulas, CHS → SFDC, SOQL"),
    ("sigma", "Sigma CLI, licenses, workbooks, datasets, user admin"),
    ("airflow", "Astro deployments, DAG deploy, DAG control, user access"),
    ("bi_apps", "Daily Pulse and other bi-applications monorepo tools"),
    ("local", "Local bi-astronomer runners (laptop)"),
    ("deploy", "SALT & Netlify production app deploys"),
    ("dbt", "dbt Core / Cloud in configured projects"),
    ("fivetran", "Individual model runs and transformation runs"),
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
    "general": "local",
}

# Tools whose registry category does not match the parent menu bucket.
_TOOL_TO_GROUP: dict[str, str] = {
    "opp-formula-refresh": "sfdc",
    "opp-formulas": "sfdc",
    "opp-formula": "sfdc",
    "sfdc-formulas": "sfdc",
    "netlify-prod-deploy": "deploy",
    "netlify-prod": "deploy",
    "netlify": "deploy",
    "salt-deploy": "deploy",
    "salt": "deploy",
    "daily-pulse": "bi_apps",
    "daily-pulse-smoke-est": "bi_apps",
    "pulse": "bi_apps",
    "de-pulse": "bi_apps",
    "pulse-smoke": "bi_apps",
}


def menu_group_for_tool(tool: Tool) -> str:
    for name in tool.names:
        if name in _TOOL_TO_GROUP:
            return _TOOL_TO_GROUP[name]
    return _CATEGORY_TO_GROUP.get(tool.category, "local")


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
        MENU_GROUPS,
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
