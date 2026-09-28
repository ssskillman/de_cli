"""Confirmation gate for high-impact menu runs."""

from __future__ import annotations

import sys

from rich.console import Console

from .registry import Tool

console = Console(stderr=True)


def confirm_menu_run(tool: Tool) -> bool:
    """Require explicit confirmation before running destructive / prod-write tools."""
    if not tool.menu_requires_confirm:
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
    console.print(
        f"Type [bold]{tool.name}[/bold] to run, or anything else to cancel:"
    )
    try:
        answer = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    if answer != tool.name:
        console.print("[dim]Cancelled.[/dim]")
        return False
    return True
