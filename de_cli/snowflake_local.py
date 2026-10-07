"""Inject laptop Snowflake SSO user for tools that read bi-astronomer .env service accounts."""

from __future__ import annotations

import os
from typing import Sequence

from .config import load_config
from .registry import Tool

_SNOWFLAKE_SSO_TOOLS = frozenset(
    {
        "opp-formula-refresh",
        "opp-formulas",
        "opp-formula",
        "sfdc-formulas",
        "customer-health-score",
        "chs",
        "account-health",
        "health-score",
        "sigma-license-audit",
        "sigma-audit",
        "departed-licenses",
    }
)

_OPP_FORMULA_TOOLS = frozenset(
    {
        "opp-formula-refresh",
        "opp-formulas",
        "opp-formula",
        "sfdc-formulas",
    }
)


def tool_uses_snowflake_sso(tool: Tool) -> bool:
    return tool.name in _SNOWFLAKE_SSO_TOOLS or any(
        alias in _SNOWFLAKE_SSO_TOOLS for alias in tool.aliases
    )


def apply_snowflake_local_user(
    tool: Tool, forwarded: Sequence[str] | None
) -> list[str]:
    """Set SNOWFLAKE_LOCAL_USER and --snowflake-user when configured in de config."""
    args = list(forwarded or ())
    if not tool_uses_snowflake_sso(tool):
        return args
    if "--snowflake-user" in args:
        return args

    user = load_config().snowflake_local_user()
    if not user:
        return args

    os.environ.setdefault("SNOWFLAKE_LOCAL_USER", user)
    os.environ.setdefault("DE_SNOWFLAKE_LOCAL_USER", user)
    if tool.name in _OPP_FORMULA_TOOLS or any(
        alias in _OPP_FORMULA_TOOLS for alias in tool.aliases
    ):
        return [*args, "--snowflake-user", user]
    return args
