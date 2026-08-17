"""Typer application: personal DE ops toolkit."""

from __future__ import annotations

import typer
from rich.console import Console

from . import __version__
from .banner import print_banner
from .doctor import run_doctor
from .registry import Tool, find_tool, tools_sorted
from .runner import interactive_menu, list_tools, run_tool_then_maybe_menu

console = Console(stderr=True)

def _tool_help(tool: Tool) -> str:
    """Primary-command help, including alias permutations."""
    if not tool.aliases:
        return tool.description
    aliases = ", ".join(f"`{a}`" for a in tool.aliases)
    # Avoid bare [...] — rich_markup_mode treats it as markup tags.
    return f"{tool.description}  (also: {aliases})"


def _alias_help(tool: Tool, alias: str) -> str:
    return f"Alias for `{tool.name}` — {tool.description}"


app = typer.Typer(
    name="de",
    help=(
        "DE ops toolkit — thin Python wrappers around CLIs and repo scripts.\n\n"
        "Run with no arguments for an interactive fzf menu (↑/↓, type to filter).\n\n"
        "Every tool name and alias is listed below (e.g. `de fivetran` == `de ft`). "
        "For wrapped tools, `-h` / `--help` is forwarded to the underlying script/CLI.\n\n"
        # \b tells Click not to rewrap this block (keeps examples one-per-line).
        "\b\n"
        "Examples:\n"
        "  de                         # fzf menu — pick a tool\n"
        "  de --list                  # table of tools + targets\n"
        "  de doctor                  # env / auth / path health check\n"
        "  de ft -h                   # Fivetran script help (alias for fivetran)\n"
        "  de fivetran --history      # recent Fivetran model runs\n"
        "  de astro-dags dev          # DAG picker on the dev deployment\n"
        "  de dags prod outlook       # alias: prefill DAG fuzzy query\n"
        "  de dbt projects            # list configured local dbt projects\n"
        "  de dbt run --select foo    # dbt in default project (config)\n"
        "  de dbt -p udi test         # dbt in the `udi` project\n"
        "  de dbt --pick run          # fzf pick project, then dbt run\n"
        "  de dbt --help              # underlying dbt CLI help (default project)\n"
        "  de astro-ls                # astro deployment list\n"
        "  de which ft                # print resolved script/CLI path\n"
        "  de salt-deploy --help      # SALT Firebase deploy help"
    ),
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)


_TOOL_CTX = {
    "allow_extra_args": True,
    "ignore_unknown_options": True,
    "help_option_names": [],
}


def _make_dispatch(tool: Tool):
    def _command(ctx: typer.Context) -> None:
        code = run_tool_then_maybe_menu(tool, list(ctx.args))
        raise typer.Exit(code)

    _command.__doc__ = tool.description
    _command.__name__ = tool.name.replace("-", "_")
    return _command


def _register_tool(tool: Tool) -> None:
    app.command(
        name=tool.name,
        help=_tool_help(tool),
        context_settings=dict(_TOOL_CTX),
    )(_make_dispatch(tool))

    # Aliases are visible in --help so every permutation is discoverable.
    for alias in tool.aliases:
        cb = _make_dispatch(tool)
        cb.__name__ = alias.replace("-", "_")
        app.command(
            name=alias,
            help=_alias_help(tool, alias),
            hidden=False,
            context_settings=dict(_TOOL_CTX),
        )(cb)


for _tool in tools_sorted():
    _register_tool(_tool)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    list_: bool = typer.Option(
        False,
        "--list",
        "-l",
        help="List available tools and exit.",
    ),
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit.",
    ),
) -> None:
    """DE ops toolkit. No args → interactive fzf picker."""
    if version:
        console.print(f"de {__version__}")
        raise typer.Exit(0)

    # Splash once per invocation (menu / list / tool commands).
    print_banner(console, compact=True)

    if list_:
        list_tools()
        raise typer.Exit(0)

    if ctx.invoked_subcommand is not None:
        return

    # Banner already printed above.
    raise typer.Exit(interactive_menu(show_banner=False))


@app.command("list")
def list_cmd() -> None:
    """List available tools."""
    list_tools()


@app.command("menu")
def menu_cmd() -> None:
    """Open the interactive fzf tool picker."""
    raise typer.Exit(interactive_menu())


@app.command("which")
def which_cmd(
    name: str = typer.Argument(..., help="Tool name or alias."),
) -> None:
    """Print the resolved command for a registered tool."""
    tool = find_tool(name)
    if tool is None:
        console.print(f"[red]Unknown tool:[/red] {name}")
        console.print("Try [bold]de --list[/bold] to see registered tools.")
        raise typer.Exit(1)
    try:
        typer.echo(tool.resolve_display_target())
    except KeyError as e:
        console.print(f"[red]ERROR:[/red] {e}")
        raise typer.Exit(1)


@app.command("doctor")
def doctor_cmd() -> None:
    """Run an environment health check before using other de tools.

    Verifies: ~/de home + venv, typer/rich imports, fzf on PATH,
    ~/.config/de/config.toml, configured repo paths (bi-astronomer / bi-dbt /
    bi-react), CLIs (astro, dbt, curl, jq), FIVETRAN_BASIC_AUTH, optional
    ASTRO_API_TOKEN, astro login via `astro deployment list`, and that every
    registered script tool resolves to an existing file.
    """
    code = run_doctor()
    from .runner import ask_return_to_menu

    if ask_return_to_menu():
        raise typer.Exit(interactive_menu())
    raise typer.Exit(code)
