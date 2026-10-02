"""fzf picker with numbered-list fallback when fzf or TTY is unavailable."""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Callable, TypeVar

from rich.console import Console

T = TypeVar("T")

console = Console(stderr=True)


def _fzf_pick(lines: list[str], *, header: str, prompt: str) -> str | None:
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
            f"--prompt={prompt}",
            "--pointer=▶",
            "--marker=◉ ",
            f"--header={header}",
            "--info=inline",
            # Up on the first row jumps to the last, and Down on the last jumps to the first.
            '--bind=up:transform:[ "$FZF_POS" -eq 1 ] && echo last || echo up',
            '--bind=down:transform:[ "$FZF_POS" -ge "$FZF_MATCH_COUNT" ] && echo first || echo down',
        ],
        input="\n".join(lines) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.split("\t", 1)[0].strip()


def _numbered_pick(
    entries: list[tuple[str, str]],
    *,
    title: str,
    allow_cancel: bool = True,
) -> str | None:
    if not entries:
        return None
    console.print(f"\n[bold]{title}[/bold]")
    for index, (_key, label) in enumerate(entries, start=1):
        console.print(f"  [cyan]{index}[/cyan]. {label}")
    if allow_cancel:
        console.print("  [dim]0. Cancel[/dim]")
    while True:
        try:
            raw = input("Choose number: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if allow_cancel and raw in {"", "0", "q"}:
            return None
        if raw.isdigit():
            num = int(raw)
            if 1 <= num <= len(entries):
                return entries[num - 1][0]
        console.print("[yellow]Invalid choice.[/yellow]")


def pick_key(
    entries: list[tuple[str, str]],
    *,
    header: str,
    prompt: str,
    list_title: str,
) -> str | None:
    """Return selected entry key from ``(key, display)`` rows."""
    if not entries:
        return None

    lines = [f"{key}\t{display}" for key, display in entries]

    if sys.stdin.isatty() and sys.stdout.isatty() and shutil.which("fzf"):
        return _fzf_pick(lines, header=header, prompt=prompt)

    if not sys.stdin.isatty():
        console.print(
            "[yellow]No interactive terminal.[/yellow] Run a tool directly, e.g. "
            "[bold]de opp-formula-refresh[/bold] or [bold]de --list[/bold]."
        )
        console.print(f"\n[bold]{list_title}[/bold]")
        for _key, label in entries:
            console.print(f"  · {label}")
        return None

    if not shutil.which("fzf"):
        console.print(
            "[dim]fzf not found — using numbered menu "
            "([bold]brew install fzf[/bold] for fuzzy search).[/dim]"
        )
    return _numbered_pick(entries, title=list_title)


def pick_tool_from_list(
    tools: list[T],
    *,
    key_fn: Callable[[T], str],
    label_fn: Callable[[T], str],
    header: str,
    prompt: str,
    list_title: str,
) -> T | None:
    entries = [(key_fn(t), label_fn(t)) for t in tools]
    chosen = pick_key(
        entries, header=header, prompt=prompt, list_title=list_title
    )
    if chosen is None:
        return None
    for tool in tools:
        if key_fn(tool) == chosen:
            return tool
    return None
