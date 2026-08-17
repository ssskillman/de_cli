"""Multi-project dbt support for `de dbt`.

Each configured project has a local ``dbt_project.yml`` cwd. Projects may use
**dbt Core** (profiles.yml) or **dbt Cloud CLI** (dbt_cloud.yml) via per-project
``engine`` / ``binary`` settings.

Configure in ``~/.config/de/config.toml``:

```toml
[dbt.projects]
bi-dbt = "bi_dbt"                 # string → path key; engine defaults to auto/PATH

[dbt.projects.udi]
path = "udi_dbt"
engine = "core"                   # core | cloud | auto
binary = "/Users/you/UDI/.venv/bin/dbt"   # optional explicit Core binary
```

Usage:

  de dbt projects                 # list configured local projects + engine
  de dbt run --select foo         # default project
  de dbt -p udi build --select stg_sfdc_account --full-refresh
  de dbt --pick test              # fzf pick project, then dbt test
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import load_config

console = Console(stderr=True)

VALID_ENGINES = frozenset({"auto", "core", "cloud"})


@dataclass(frozen=True)
class DbtProject:
    name: str
    path: Path
    engine: str = "auto"  # auto | core | cloud
    binary: Path | None = None

    @property
    def has_project_yml(self) -> bool:
        return (self.path / "dbt_project.yml").is_file()


def _resolve_path_value(value: str) -> Path:
    cfg = load_config()
    path = Path(str(value)).expanduser()
    # Allow path-key references: bi_dbt → [paths].bi_dbt
    if not path.is_absolute() and "/" not in str(value) and "\\" not in str(value):
        keyed = cfg.path(str(value))
        if keyed is not None:
            path = keyed
    return path


def _parse_project_entry(name: str, value: object) -> DbtProject:
    if isinstance(value, str):
        return DbtProject(name=name, path=_resolve_path_value(value), engine="auto")

    if isinstance(value, dict):
        raw_path = value.get("path")
        if not raw_path:
            raise ValueError(f"[dbt.projects.{name}] missing required 'path'")
        engine = str(value.get("engine") or "auto").lower().strip()
        if engine not in VALID_ENGINES:
            raise ValueError(
                f"[dbt.projects.{name}].engine={engine!r} "
                f"(expected one of: {', '.join(sorted(VALID_ENGINES))})"
            )
        binary: Path | None = None
        if value.get("binary"):
            binary = Path(str(value["binary"])).expanduser()
        return DbtProject(
            name=name,
            path=_resolve_path_value(str(raw_path)),
            engine=engine,
            binary=binary,
        )

    raise ValueError(
        f"[dbt.projects.{name}] must be a path string or a table "
        f"with path=/engine=/binary="
    )


def configured_projects() -> list[DbtProject]:
    """Projects from config [dbt.projects], falling back to paths.bi_dbt."""
    cfg = load_config()
    raw = cfg.dbt_projects
    projects: list[DbtProject] = []

    if raw:
        for name, value in raw.items():
            try:
                projects.append(_parse_project_entry(name, value))
            except ValueError as e:
                console.print(f"[red]ERROR:[/red] {e}")
    else:
        p = cfg.path("bi_dbt")
        if p is not None:
            projects.append(DbtProject(name="bi-dbt", path=p, engine="auto"))

    return projects


def default_project_name() -> str | None:
    return load_config().defaults.get("dbt_project") or None


def find_project(name: str) -> DbtProject | None:
    key = name.lower().strip()
    for p in configured_projects():
        if p.name.lower() == key:
            return p
    return None


def _candidate_core_binaries(project: DbtProject) -> list[Path]:
    root = project.path
    return [
        root / ".venv" / "bin" / "dbt",
        root.parent / ".venv" / "bin" / "dbt",  # UDI monorepo: UDI/.venv
        root / "venv" / "bin" / "dbt",
    ]


def _looks_like_core(binary: Path) -> bool:
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    out = (proc.stdout or "") + (proc.stderr or "")
    if "dbt Cloud CLI" in out:
        return False
    return "Core:" in out or proc.returncode == 0


def resolve_dbt_binary(
    project: DbtProject,
    *,
    quiet: bool = False,
) -> Path | None:
    """Return the dbt executable for this project, or None."""
    if project.binary is not None:
        if project.binary.is_file() and os.access(project.binary, os.X_OK):
            return project.binary
        if not quiet:
            console.print(
                f"[red]ERROR:[/red] configured binary not found/executable: "
                f"{project.binary}"
            )
        return None

    engine = project.engine

    if engine in ("core", "auto"):
        for candidate in _candidate_core_binaries(project):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                if engine == "core" or _looks_like_core(candidate):
                    return candidate
        if engine == "core":
            if not quiet:
                looked = "\n  ".join(str(p) for p in _candidate_core_binaries(project))
                console.print(
                    f"[red]ERROR:[/red] no dbt Core binary found for project "
                    f"[bold]{project.name}[/bold].\n"
                    f"Set [bold]\\[dbt.projects.{project.name}].binary[/bold] "
                    f"to your venv dbt (e.g. ~/UDI/.venv/bin/dbt).\n"
                    f"Looked in:\n  {looked}"
                )
            return None

    # cloud (or auto with no local Core) → prefer Homebrew Cloud CLI, else PATH
    if engine == "cloud":
        for candidate in (
            Path("/opt/homebrew/bin/dbt"),
            Path("/usr/local/bin/dbt"),
        ):
            if candidate.is_file() and os.access(candidate, os.X_OK):
                if not _looks_like_core(candidate):
                    return candidate

    which = shutil.which("dbt")
    if which:
        path = Path(which)
        if engine == "cloud" and _looks_like_core(path):
            if not quiet:
                console.print(
                    "[red]ERROR:[/red] engine=cloud but PATH resolves to dbt Core "
                    f"({path}). Install dbt Cloud CLI or put it ahead of Core on PATH."
                )
            return None
        return path

    if not quiet:
        console.print(
            "[red]ERROR:[/red] `dbt` not found on PATH. "
            "Install dbt Cloud CLI or set a Core [bold]binary[/bold] "
            "for this project."
        )
    return None


def cloud_credentials_path() -> Path:
    return Path.home() / ".dbt" / "dbt_cloud.yml"


def has_cloud_credentials() -> bool:
    return cloud_credentials_path().is_file()


def ensure_cloud_ready(project: DbtProject, *, quiet: bool = False) -> bool:
    """Cloud CLI projects need ~/.dbt/dbt_cloud.yml (not profiles.yml)."""
    if project.engine not in ("cloud", "auto"):
        return True
    # Only enforce when we actually resolved to Cloud CLI.
    binary = resolve_dbt_binary(project, quiet=True)
    if binary is None:
        return False
    if _looks_like_core(binary):
        return True
    if has_cloud_credentials():
        return True
    if not quiet:
        cred = cloud_credentials_path()
        console.print(
            f"[red]ERROR:[/red] project [bold]{project.name}[/bold] uses "
            f"[bold]dbt Cloud CLI[/bold], but credentials are missing:\n"
            f"  {cred}\n\n"
            f"[bold]Fix (pick one):[/bold]\n"
            f"  1. In dbt Cloud → account settings / CLI → download "
            f"[bold]dbt_cloud.yml[/bold] into [bold]~/.dbt/[/bold]\n"
            f"  2. Run bi-dbt in the [bold]dbt Cloud IDE[/bold] instead of local CLI\n"
            f"  3. For UDI local Core (profiles.yml), use "
            f"[bold]de dbt -p udi …[/bold]\n\n"
            f"[dim]UDI does not need dbt_cloud.yml. bi-dbt Cloud CLI does.[/dim]"
        )
    return False


def describe_binary(binary: Path) -> str:
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return f"{binary} (unreadable)"
    out = (proc.stdout or "") + (proc.stderr or "")
    if "dbt Cloud CLI" in out:
        first = next((ln for ln in out.splitlines() if ln.strip()), "Cloud CLI")
        return f"cloud · {first.strip()}"
    if "Core:" in out:
        ver = next(
            (ln.strip() for ln in out.splitlines() if "installed:" in ln.lower()),
            "Core",
        )
        return f"core · {ver}"
    return str(binary)


def list_projects() -> int:
    projects = configured_projects()
    table = Table(title="de dbt projects", show_header=True, header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Default")
    table.add_column("dbt_project.yml")
    table.add_column("Engine")
    table.add_column("Binary", overflow="fold")
    table.add_column("Path", overflow="fold")

    default = default_project_name()
    if not projects:
        console.print(
            "[yellow]No dbt projects configured.[/yellow] "
            "Add a [bold]\\[dbt.projects][/bold] table in ~/.config/de/config.toml"
        )
        return 1

    for p in projects:
        is_default = "yes" if default and p.name.lower() == default.lower() else ""
        yml = "[green]ok[/green]" if p.has_project_yml else "[red]missing[/red]"
        binary = None
        if p.has_project_yml and p.path.is_dir():
            binary = resolve_dbt_binary(p, quiet=True)
        bin_label = describe_binary(binary) if binary else "[red]unresolved[/red]"
        table.add_row(
            p.name,
            is_default,
            yml,
            p.engine,
            bin_label,
            str(p.path),
        )

    console.print(table)
    console.print(
        "\n[dim]Usage: de dbt -p NAME <args…>   ·   de dbt --pick <args…>   ·   "
        "default from config defaults.dbt_project[/dim]"
    )
    console.print(
        "[dim]UDI Core example: de dbt -p udi build --select "
        "stg_sfdc_account --full-refresh[/dim]"
    )
    return 0


def fzf_pick_project() -> DbtProject | None:
    projects = configured_projects()
    if not projects:
        console.print("[red]ERROR:[/red] no dbt projects in config.")
        return None
    if len(projects) == 1:
        return projects[0]

    if not shutil.which("fzf"):
        console.print("[red]ERROR:[/red] fzf not found (brew install fzf)")
        return None
    if not sys.stdin.isatty():
        console.print(
            "[red]ERROR:[/red] multiple dbt projects — pass "
            "[bold]-p NAME[/bold] or run in a TTY for --pick"
        )
        return None

    default = default_project_name()
    lines: list[str] = []
    for p in projects:
        mark = " (default)" if default and p.name.lower() == default.lower() else ""
        yml = "ok" if p.has_project_yml else "NO dbt_project.yml"
        lines.append(
            f"{p.name}\t{p.name:<16}  {yml:<18}  engine={p.engine:<5}  {p.path}{mark}"
        )

    proc = subprocess.run(
        [
            "fzf",
            "--ansi",
            "--height=40%",
            "--reverse",
            "--border=rounded",
            "--cycle",
            "--delimiter=\t",
            "--with-nth=2",
            "--prompt=dbt project > ",
            "--header=Pick a local dbt project (needs dbt_project.yml)",
        ],
        input="\n".join(lines) + "\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    name = proc.stdout.split("\t", 1)[0].strip()
    return find_project(name)


def resolve_project(
    *,
    name: str | None = None,
    pick: bool = False,
) -> DbtProject | None:
    if pick:
        return fzf_pick_project()

    if name:
        proj = find_project(name)
        if proj is None:
            known = ", ".join(p.name for p in configured_projects()) or "(none)"
            console.print(f"[red]Unknown dbt project:[/red] {name}")
            console.print(f"Configured: {known}")
            console.print("Run [bold]de dbt projects[/bold] to list.")
        return proj

    default = default_project_name()
    if default:
        proj = find_project(default)
        if proj is None:
            console.print(
                f"[red]defaults.dbt_project={default!r} not in [dbt.projects][/red]"
            )
            return None
        return proj

    projects = configured_projects()
    if len(projects) == 1:
        return projects[0]
    if len(projects) == 0:
        console.print(
            "[red]ERROR:[/red] no dbt projects configured. "
            "Add [bold]\\[dbt.projects][/bold] in ~/.config/de/config.toml"
        )
        return None

    return fzf_pick_project()


def prompt_dbt_command(project: DbtProject) -> list[str] | None:
    """Interactive: pick a common command or type custom dbt args."""
    presets: list[tuple[str, str]] = []
    if project.name.lower() == "udi":
        presets = [
            (
                "udi-account-full-refresh",
                "build --select stg_sfdc_account --full-refresh",
            ),
            (
                "udi-account-incremental",
                "build --select stg_sfdc_account",
            ),
            ("udi-staging", "build --select staging"),
            ("debug", "debug"),
            ("version", "--version"),
        ]
    else:
        presets = [
            ("build", "build"),
            ("run", "run"),
            ("test", "test"),
            ("debug", "debug"),
            ("version", "--version"),
        ]
    presets.append(("custom", "(type your own dbt args)"))

    chosen: str | None = None
    if shutil.which("fzf") and sys.stdin.isatty():
        lines = [f"{key}\t{key:<28}  {cmd}" for key, cmd in presets]
        proc = subprocess.run(
            [
                "fzf",
                "--ansi",
                "--height=40%",
                "--reverse",
                "--border=rounded",
                "--cycle",
                "--delimiter=\t",
                "--with-nth=2",
                f"--prompt=dbt {project.name} > ",
                "--header=Pick a command (or custom). Esc cancels.",
            ],
            input="\n".join(lines) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            console.print("[dim]Cancelled.[/dim]")
            return None
        chosen = proc.stdout.split("\t", 1)[0].strip()
    else:
        console.print("[bold]Common commands[/bold]")
        for key, cmd in presets:
            console.print(f"  [cyan]{key}[/cyan]  {cmd}")
        sys.stderr.write("preset name (or custom): ")
        sys.stderr.flush()
        chosen = sys.stdin.readline().strip() or None
        if not chosen:
            return None

    if chosen == "custom":
        console.print(
            "[dim]Example:[/dim] build --select stg_sfdc_account --full-refresh"
        )
        sys.stderr.write(f"dbt {project.name} args> ")
        sys.stderr.flush()
        line = sys.stdin.readline()
        if not line or not line.strip():
            console.print("[dim]Cancelled (empty args).[/dim]")
            return None
        return shlex.split(line.strip())

    for key, cmd in presets:
        if key == chosen:
            if not cmd or cmd.startswith("("):
                continue
            return shlex.split(cmd)

    console.print(f"[red]Unknown preset:[/red] {chosen}")
    return None


def parse_dbt_args(
    args: list[str],
) -> tuple[list[str] | None, str | None, bool, bool]:
    """Strip de-owned flags from forwarded dbt args.

    Returns (remaining_args, project_name, pick, list_projects_only).
    ``remaining_args`` is None on parse error (message already printed).
    """
    remaining: list[str] = []
    project: str | None = None
    pick = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("projects", "--projects") and i == 0 and not remaining:
            return [], None, False, True
        if a in ("--pick", "-P"):
            pick = True
            i += 1
            continue
        if a in ("-p", "--project"):
            if i + 1 >= len(args):
                console.print("[red]ERROR:[/red] -p/--project needs a project name")
                console.print("Try [bold]de dbt projects[/bold] to list names.")
                return None, None, False, False
            project = args[i + 1]
            i += 2
            continue
        if a.startswith("--project="):
            project = a.split("=", 1)[1]
            i += 1
            continue
        remaining.append(a)
        i += 1
    return remaining, project, pick, False
