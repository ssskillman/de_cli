"""Menu group reference (`de groups`) and in-menu discoverability."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .menu_groups import (
    MENU_GROUPS,
    menu_group_for_tool,
    menu_group_label,
    tool_menu_description,
    tools_in_menu_group,
)
from .registry import tools_sorted


def print_menu_groups_guide(console: Console | None = None) -> None:
    """Doctor-style overview of menu groups and tools."""
    c = console or Console(stderr=True)
    c.print("[bold]de menu groups[/bold] — what each bucket is for\n")

    for key, _subtitle, blurb in MENU_GROUPS:
        label = menu_group_label(key)
        tools = tools_in_menu_group(key)
        c.print(f"[bold cyan]{label}[/bold cyan] [dim]({key})[/dim]")
        c.print(f"  {blurb}\n")

        table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
        table.add_column("Tool", style="bold")
        table.add_column("Summary")
        table.add_column("Note", style="yellow")
        for tool in tools:
            table.add_row(
                tool.name,
                tool_menu_description(tool),
                tool.menu_hint or "—",
            )
        c.print(table)
        c.print()

    ungrouped = [
        t
        for t in tools_sorted()
        if menu_group_for_tool(t) not in {g[0] for g in MENU_GROUPS}
    ]
    if ungrouped:
        c.print("[yellow]Ungrouped tools:[/yellow]", ", ".join(t.name for t in ungrouped))

    c.print(
        "\n[dim]Tip:[/dim] [bold]de[/bold] opens the menu · "
        "[bold]de groups[/bold] prints this guide · "
        "[bold]de --list[/bold] shows CLI targets"
    )
