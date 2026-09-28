"""Minimal header for the `de` CLI."""

from __future__ import annotations

import os

from rich.console import Console

from . import __version__


def print_banner(
    console: Console | None = None,
    *,
    compact: bool = False,
    repeat: bool = False,
) -> None:
    """Optional one-line version stamp (ASCII art removed).

    ``DE_NO_BANNER=1`` — print nothing.
    """
    if os.environ.get("DE_NO_BANNER") == "1":
        return
    c = console or Console(stderr=True)
    c.print(f"[bold cyan]de[/bold cyan] [dim]v{__version__}[/dim]", highlight=False)
