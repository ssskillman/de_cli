"""Execute registered tools and drive the interactive fzf menu."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .banner import print_banner
from .menu_confirm import confirm_menu_run, daily_pulse_smoke_ready
from .menu_groups import (
    breadcrumb,
    menu_group_for_tool,
    menu_group_label,
    ordered_menu_groups,
    tool_menu_line,
    tools_by_names,
    tools_in_menu_group,
)
from .menu_guide import group_blurb, print_menu_groups_guide
from .menu_pick import pick_key, pick_tool_from_list
from .menu_state import (
    ALL_TOOLS_KEY,
    GUIDE_KEY,
    PINNED_KEY,
    RECENT_KEY,
    push_recent_tool,
    read_pinned_tool_names,
    read_recent_tool_names,
    write_last_menu_group,
)
from .registry import Tool, tools_sorted
from .snowflake_local import apply_snowflake_local_user

console = Console(stderr=True)

# Child tools (e.g. sf-opp-query) exit with this to jump straight to the de menu.
RETURN_TO_MENU_CODE = 10


def run_tool(tool: Tool, args: Sequence[str] | None = None) -> int:
    """Run a tool via subprocess (keeps `de` alive so we can return to the menu)."""
    forwarded: list[str] = list(args or ())
    if not forwarded and tool.default_args:
        forwarded = list(tool.default_args)
        console.print(
            f"[dim]no args — using defaults:[/dim] [bold]{tool.name} "
            f"{' '.join(forwarded)}[/bold]"
        )
    forwarded = apply_snowflake_local_user(tool, forwarded)

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
            _explain_exec_failure(binary)
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
        _explain_exec_failure(Path(executable))
        return 1


def _explain_exec_failure(executable: Path) -> None:
    """ENOENT on an existing script usually means its `#!` interpreter is gone."""
    if not executable.is_file():
        return
    from .dbt_projects import broken_binary_advice, interpreter_problem

    problem = interpreter_problem(executable)
    if problem is None:
        return
    console.print(f"[yellow]Cause:[/yellow] the file exists, but {problem}.")
    console.print(broken_binary_advice(executable))


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


def pause_after_tool() -> bool:
    """Pause after running a tool so the user can inspect output and scroll.

    Returns True to continue in the menu, False to exit.
    """
    if not sys.stdin.isatty():
        return True
    try:
        console.print("\n[dim]Press Enter to return to menu (or 'q' to quit)...[/dim]", end="")
        sys.stderr.flush()
        ans = input().strip().lower()
        return ans not in ("q", "quit", "exit")
    except (EOFError, KeyboardInterrupt):
        return False


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
        except Exception:
            target = tool.script or " ".join(tool.argv or ())
        table.add_row(
            tool.name,
            ", ".join(tool.aliases) if tool.aliases else "—",
            tool.category,
            target,
            tool.description,
        )
    console.print(table)


_HDR_NAV = "__hdr_nav__"
_HDR_PLAT = "__hdr_plat__"
_MENU_HEADINGS = frozenset({_HDR_NAV, _HDR_PLAT})


def _menu_heading(label: str) -> str:
    return f"\x1b[1;36m── {label}\x1b[0m"


def _menu_group_entries() -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = [(_HDR_NAV, _menu_heading("Navigate"))]
    total = len(tools_sorted())
    entries.append(
        (
            ALL_TOOLS_KEY,
            f"{'All tools':<12}  ({total} tools)  Search the full registry",
        )
    )
    pinned = tools_by_names(read_pinned_tool_names())
    if pinned:
        entries.append(
            (
                PINNED_KEY,
                f"{'Pinned':<12}  ({len(pinned)} tools)  [menu] pinned in config.toml",
            )
        )
    recent = tools_by_names(read_recent_tool_names())
    if recent:
        entries.append(
            (
                RECENT_KEY,
                f"{'Recent':<12}  ({len(recent)} tools)  Last tools you ran",
            )
        )
    entries.append(
        (
            GUIDE_KEY,
            f"{'Guide':<12}         What each group is for (like de groups)",
        )
    )
    entries.append((_HDR_PLAT, _menu_heading("Platforms")))
    for key, subtitle in ordered_menu_groups():
        count = len(tools_in_menu_group(key))
        label = menu_group_label(key)
        entries.append(
            (key, f"{label:<12}  ({count} tools)  {subtitle}")
        )
    return entries


def pick_menu_group() -> str | None:
    while True:
        key = pick_key(
            _menu_group_entries(),
            header="Pick a group  ·  ↑/↓  ·  filter  ·  Enter  ·  Esc cancel",
            prompt="de group > ",
            list_title="de menu groups",
        )
        if key in _MENU_HEADINGS:
            continue
        return key


def _tools_for_menu_group(menu_group: str | None) -> tuple[Tool, ...]:
    if menu_group == PINNED_KEY:
        return tools_by_names(read_pinned_tool_names())
    if menu_group == RECENT_KEY:
        return tools_by_names(read_recent_tool_names())
    if menu_group == ALL_TOOLS_KEY:
        return tools_sorted()
    if menu_group:
        return tools_in_menu_group(menu_group)
    return tools_sorted()


def menu_command_name(tool: Tool, group_key: str | None) -> str:
    """Inside a group, hide the repeated ``sigma-`` / ``fivetran-`` prefix."""
    name = tool.name
    if group_key and name.startswith(f"{group_key}-"):
        return name[len(group_key) + 1 :]
    return name


def tool_menu_view(
    tools: Sequence[Tool],
    *,
    show_group: bool,
    include_hint: bool = False,
    group_key: str | None = None,
) -> tuple[str, list[str]]:
    """Aligned command rows. A single group does not repeat its name on every line."""
    names = [
        tool.name if show_group else menu_command_name(tool, group_key)
        for tool in tools
    ]
    name_width = max((len(name) for name in names), default=0)
    summaries = [
        tool_menu_line(tool, include_hint=include_hint) for tool in tools
    ]
    controls = "↑/↓ move  ·  type to filter  ·  enter run  ·  esc back"
    if show_group:
        groups = [menu_group_label(menu_group_for_tool(tool)) for tool in tools]
        group_width = max((len(group) for group in groups), default=0)
        columns = f"{'command':<{name_width}}  {'group':<{group_width}}  action"
        rows = [
            f"{name:<{name_width}}  {group:<{group_width}}  {summary}"
            for name, group, summary in zip(names, groups, summaries)
        ]
    else:
        columns = f"{'command':<{name_width}}  action"
        rows = [
            f"{name:<{name_width}}  {summary}"
            for name, summary in zip(names, summaries)
        ]
    return f"{controls}\n{columns}", rows


def pick_tool(menu_group: str | None = None) -> Tool | None:
    tools = list(_tools_for_menu_group(menu_group))
    if not tools:
        console.print("[yellow]No tools in this group.[/yellow]")
        return None

    show_hints = menu_group in {RECENT_KEY, PINNED_KEY}
    single_group = bool(
        menu_group and menu_group not in {ALL_TOOLS_KEY, RECENT_KEY, PINNED_KEY}
    )

    blurb = group_blurb(menu_group) if single_group else None
    if blurb:
        console.print(f"[dim]{blurb}[/dim]\n")

    header, labels = tool_menu_view(
        tools,
        show_group=not single_group,
        include_hint=show_hints,
        group_key=menu_group if single_group else None,
    )
    labels_by_name = dict(zip((tool.name for tool in tools), labels))
    title = menu_group_label(menu_group) if menu_group else "Tools"
    return pick_tool_from_list(
        tools,
        key_fn=lambda t: t.name,
        label_fn=lambda tool: labels_by_name[tool.name],
        header=header,
        prompt=f"de {menu_group or 'tools'} > ",
        list_title=f"Tools · {title}",
        border_label=title if single_group else "",
    )


def interactive_menu(
    *,
    forward_args: Sequence[str] | None = None,
    show_banner: bool = True,
) -> int:
    """Show fzf menu, run tools, and optionally return to the menu after each run."""
    if show_banner:
        print_banner(console)

    last_code = 0
    group: str | None = None
    while True:
        if group is None:
            group = pick_menu_group()
            if group is None:
                console.print("[dim]Cancelled.[/dim]")
                return last_code

            if group == GUIDE_KEY:
                print_menu_groups_guide(console)
                group = None
                continue

            if group not in {ALL_TOOLS_KEY, RECENT_KEY, PINNED_KEY, GUIDE_KEY}:
                write_last_menu_group(group)

        tool = pick_tool(group)
        if tool is None:
            # Esc on the tool list: one level up, back to groups.
            group = None
            continue

        console.print(f"[bold]{breadcrumb(group, tool.name)}[/bold]")
        hide_token_hint = (
            tool.name == "daily-pulse-smoke-est" and daily_pulse_smoke_ready()
        )
        if (
            tool.menu_hint
            and group not in {RECENT_KEY, PINNED_KEY}
            and not hide_token_hint
        ):
            console.print(f"[yellow]{tool.menu_hint}[/yellow]")
        if not confirm_menu_run(tool):
            continue

        push_recent_tool(tool.name)

        last_code = run_tool(tool, forward_args)

        # Tool asked for the top menu (e.g. sf-opp-query "back to de").
        if last_code == RETURN_TO_MENU_CODE:
            last_code = 0
            group = None
            print_banner(console)
            continue

        if not pause_after_tool():
            return last_code

        # Stay on this group's tool list. Esc there returns to groups.
        # Do not ask "Return to de menu?" — that jumps to the top.


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
        return interactive_menu(show_banner=False)
    if ask_return_to_menu():
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            console.print(
                "[yellow]No TTY for interactive menu.[/yellow] "
                "Run [bold]de[/bold] in a terminal to open the picker."
            )
            return code
        return interactive_menu(show_banner=False)
    return code
