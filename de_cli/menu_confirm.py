"""Confirmation gate for high-impact menu runs."""

from __future__ import annotations

import sys
from pathlib import Path

from rich.console import Console

from .registry import Tool

console = Console(stderr=True)

_PULSE_SMOKE = "daily-pulse-smoke-est"


def slack_bot_token_in_env(env_file: Path) -> bool:
    """True when SLACK_BOT_TOKEN is set to a non-empty value in an env file."""
    if not env_file.is_file():
        return False
    try:
        lines = env_file.read_text(errors="replace").splitlines()
    except OSError:
        return False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        if key.strip() != "SLACK_BOT_TOKEN":
            continue
        token = value.strip()
        if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
            token = token[1:-1].strip()
        return bool(token)
    return False


def daily_pulse_smoke_ready(env_file: Path | None = None) -> bool:
    """True when bi-applications .env already has a Slack bot token."""
    if env_file is None:
        from .config import load_config

        root = load_config().path("bi_applications")
        if root is None:
            return False
        env_file = root / ".env"
    return slack_bot_token_in_env(env_file)


def confirm_menu_run(tool: Tool) -> bool:
    """Require explicit confirmation before running destructive / prod-write tools."""
    if not tool.menu_requires_confirm:
        return True
    # Posting the smoke pulse is the impact. A present token is the opt-in.
    if tool.name == _PULSE_SMOKE and daily_pulse_smoke_ready():
        return True
    if not sys.stdin.isatty():
        console.print(
            "[red]Refusing to run[/red] [bold]"
            f"{tool.name}[/bold] without a TTY confirmation. "
            f"Run [bold]de {tool.name}[/bold] directly and pass explicit flags."
        )
        return False

    console.print(
        f"\n[bold red]Confirm[/bold red] [bold]{tool.name}[/bold] "
        "(production or deploy impact)"
    )
    if tool.menu_hint:
        console.print(f"[yellow]{tool.menu_hint}[/yellow]")
    console.print("[dim]Press Enter to run, or N to cancel.[/dim]")
    try:
        answer = input("> ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer in {"n", "no"}:
        console.print("[dim]Cancelled.[/dim]")
        return False
    return True
