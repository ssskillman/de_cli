"""Splash banner for the `de` CLI (ANSI Compact — Iterable DE / -shawn)."""

from __future__ import annotations

from rich.console import Console

# From https://patorjk.com/software/taag/ — font: ANSI Compact
# Text: "Iterable DE\n-shawn"
_BANNER = r"""
██ ▄▄▄▄▄▄ ▄▄▄▄▄ ▄▄▄▄   ▄▄▄  ▄▄▄▄  ▄▄    ▄▄▄▄▄   ████▄  ██████
██   ██   ██▄▄  ██▄█▄ ██▀██ ██▄██ ██    ██▄▄    ██  ██ ██▄▄
██   ██   ██▄▄▄ ██ ██ ██▀██ ██▄█▀ ██▄▄▄ ██▄▄▄   ████▀  ██▄▄▄▄

     ▄▄▄▄ ▄▄ ▄▄  ▄▄▄  ▄▄   ▄▄ ▄▄  ▄▄
▄▄▄ ███▄▄ ██▄██ ██▀██ ██ ▄ ██ ███▄██
    ▄▄██▀ ██ ██ ██▀██  ▀█▀█▀  ██ ▀██
"""

# Same wordmark; used on every `de` invocation (menu / commands).
_BANNER_COMPACT = _BANNER


def print_banner(console: Console | None = None, *, compact: bool = False) -> None:
    """Print the Iterable DE ASCII splash to stderr (or the given console)."""
    c = console or Console(stderr=True)
    art = _BANNER_COMPACT if compact else _BANNER
    c.print(f"[bold cyan]{art}[/bold cyan]", highlight=False)
