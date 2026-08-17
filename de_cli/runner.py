"""Execute registered tools and drive the interactive fzf menu."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from rich.console import Console
from rich.table import Table

from .banner import print_banner
from .registry import Tool, find_tool, tools_sorted

console = Console(stderr=True)

# Child tools (e.g. sf-opp-query) exit with this to jump straight to the de menu.
RETURN_TO_MENU_CODE = 10


def run_tool(tool: Tool, args: Sequence[str] | None = None) -> int:
    """Run a tool via subprocess (keeps `de` alive so we can return to the menu)."""
    forwarded: list[str] = list(args or ())

    # Multi-project dbt: strip -p/--project/--pick; resolve local project cwd + binary.
    if tool.name == "dbt":
        from .dbt_projects import (
            list_projects,
            parse_dbt_args,
            resolve_dbt_binary,
            resolve_project,
        )

        remaining, project_name, pick, list_only = parse_dbt_args(forwarded)
        if list_only:
            return list_projects()
        if remaining is None:
            return 1

        # Menu path (`de` → pick dbt) has no args: pick project + command.
        interactive = (
            not remaining
            and project_name is None
            and not pick
            and sys.stdin.isatty()
        )
        if interactive:
            pick = True

        proj = resolve_project(name=project_name, pick=pick)
        if proj is None:
            return 1
        if not proj.path.is_dir():
            console.print(f"[red]ERROR:[/red] project path missing: {proj.path}")
            return 1
        if not proj.has_project_yml:
            console.print(
                f"[red]ERROR:[/red] no dbt_project.yml in {proj.path}\n"
                "dbt must run from a local dbt project directory."
            )
            return 1

        if interactive:
            from .dbt_projects import prompt_dbt_command

            remaining = prompt_dbt_command(proj)
            if remaining is None:
                return 1

        if not remaining:
            console.print(
                "[yellow]No dbt args.[/yellow] Examples:\n"
                "  [bold]de dbt -p udi build --select stg_sfdc_account "
                "--full-refresh[/bold]\n"
                "  [bold]de dbt -p udi build --select stg_sfdc_account[/bold]\n"
                "  [bold]de dbt projects[/bold]"
            )
            return 1

        from .dbt_projects import ensure_cloud_ready

        if not ensure_cloud_ready(proj):
            return 1

        binary = resolve_dbt_binary(proj)
        if binary is None:
            return 1
        console.print(
            f"[dim]dbt project:[/dim] [bold]{proj.name}[/bold]  "
            f"[dim]{proj.path}[/dim]\n"
            f"[dim]dbt binary:[/dim]  {binary}  [dim](engine={proj.engine})[/dim]\n"
            f"[dim]command:[/dim]     dbt {' '.join(remaining)}"
        )
        cmd = [str(binary), *remaining]
        try:
            return int(subprocess.call(cmd, cwd=str(proj.path)))
        except OSError as e:
            console.print(f"[red]ERROR:[/red] failed to run {cmd[0]}: {e}")
            return 1

    try:
        cmd = tool.resolve_command()
    except KeyError as e:
        console.print(f"[red]ERROR:[/red] {e}")
        return 1

    cmd = [*cmd, *forwarded]
    executable = cmd[0]

    # Script paths must exist; bare CLIs are resolved via PATH.
    if tool.script is not None:
        path = Path(executable)
        if not path.exists():
            console.print(f"[red]ERROR:[/red] script not found: {path}")
            return 1
        if not os.access(path, os.X_OK):
            path.chmod(path.stat().st_mode | 0o111)

    cwd: Path | None = None
    try:
        cwd = tool.resolve_cwd()
    except KeyError as e:
        console.print(f"[red]ERROR:[/red] {e}")
        return 1

    if cwd is not None and not cwd.is_dir():
        console.print(f"[red]ERROR:[/red] cwd does not exist: {cwd}")
        return 1

    try:
        return int(subprocess.call(cmd, cwd=str(cwd) if cwd else None))
    except OSError as e:
        console.print(f"[red]ERROR:[/red] failed to run {cmd[0]}: {e}")
        return 1


def ask_return_to_menu() -> bool:
    """Ask whether to reopen the main de fzf menu. Default: yes."""
    if os.environ.get("DE_SKIP_MENU_PROMPT") == "1":
        return False

    console.print()
    sys.stderr.write("Return to de menu? [Y/n] ")
    sys.stderr.flush()
    try:
        ans = sys.stdin.readline()
    except EOFError:
        return False
    if ans == "":
        # EOF
        return False
    ans = ans.strip().lower()
    return ans in ("", "y", "yes")


def list_tools(*, as_table: bool = True) -> None:
    tools = tools_sorted()
    if not as_table:
        for tool in tools:
            alias = f" [{', '.join(tool.aliases)}]" if tool.aliases else ""
            print(f"{tool.name}{alias}\t{tool.description}")
        return

    table = Table(title="de tools", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Aliases", style="dim")
    table.add_column("Category")
    table.add_column("Target", style="dim")
    table.add_column("Description")
    for tool in tools:
        try:
            target = tool.resolve_display_target()
        except Exception:  # noqa: BLE001
            target = tool.script or " ".join(tool.argv or ())
        table.add_row(
            tool.name,
            ", ".join(tool.aliases) if tool.aliases else "—",
            tool.category,
            target,
            tool.description,
        )
    console.print(table)


def fzf_pick_tool() -> Tool | None:
    """Interactive ↑/↓ fuzzy picker over the tool registry (alpha by name)."""
    if not shutil.which("fzf"):
        console.print(
            "[red]ERROR:[/red] fzf not found. Install with: [bold]brew install fzf[/bold]"
        )
        return None

    lines: list[str] = []
    for tool in tools_sorted():
        alias_bit = f"  aliases: {', '.join(tool.aliases)}" if tool.aliases else ""
        display = (
            f"{tool.name:<22}  {tool.category:<10}  {tool.description}{alias_bit}"
        )
        lines.append(f"{tool.name}\t{display}")

    header = (
        "Pick a tool  ·  ↑/↓ navigate  ·  type to filter  ·  Enter run  ·  Esc cancel"
    )
    proc = subprocess.run(
        [
            "fzf",
            "--ansi",
            "--height=60%",
            "--reverse",
            "--border=rounded",
            "--cycle",
            "--delimiter=\t",
            "--with-nth=2",
            "--prompt=de > ",
            "--pointer=▶",
            "--marker=◉ ",
            f"--header={header}",
            "--info=inline",
        ],
        input="\n".join(lines) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None

    name = proc.stdout.split("\t", 1)[0].strip()
    return find_tool(name)


def interactive_menu(
    *,
    forward_args: Sequence[str] | None = None,
    show_banner: bool = True,
) -> int:
    """Show fzf menu, run tools, and optionally return to the menu after each run."""
    if show_banner:
        print_banner(console, compact=True)

    last_code = 0
    while True:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            console.print(
                "[yellow]No TTY for interactive menu.[/yellow] "
                "Use [bold]de --list[/bold] or [bold]de <tool>[/bold]."
            )
            if last_code == 0 and forward_args is None:
                list_tools()
                return 1
            return last_code

        tool = fzf_pick_tool()
        if tool is None:
            console.print("[dim]Cancelled.[/dim]")
            return last_code

        console.print(
            f"[cyan]→[/cyan] [bold]{tool.name}[/bold]  [dim]{tool.description}[/dim]"
        )
        last_code = run_tool(tool, forward_args)

        # Tool requested the menu directly (e.g. sf-opp-query "back to de").
        if last_code == RETURN_TO_MENU_CODE:
            last_code = 0
            print_banner(console, compact=True)
            continue

        if ask_return_to_menu():
            print_banner(console, compact=True)
            continue
        return last_code


def run_tool_then_maybe_menu(
    tool: Tool, args: Sequence[str] | None = None
) -> int:
    """Run a named tool (`de astro-ls`), then optionally open the main menu."""
    code = run_tool(tool, args)
    if code == RETURN_TO_MENU_CODE:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            console.print(
                "[yellow]No TTY for interactive menu.[/yellow] "
                "Run [bold]de[/bold] in a terminal to open the picker."
            )
            return 0
        return interactive_menu()
    if ask_return_to_menu():
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            console.print(
                "[yellow]No TTY for interactive menu.[/yellow] "
                "Run [bold]de[/bold] in a terminal to open the picker."
            )
            return code
        return interactive_menu()
    return code
