"""de doctor — env / secrets / path checklist."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import CONFIG_PATH, DE_HOME, load_config
from .registry import TOOLS

console = Console(stderr=True)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _which(name: str) -> str | None:
    return shutil.which(name)


def _run(cmd: list[str], timeout: float = 15) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode, out
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def run_doctor() -> int:
    cfg = load_config(reload=True)
    checks: list[Check] = []

    # --- runtime ---
    venv_py = DE_HOME / ".venv" / "bin" / "python"
    checks.append(
        Check(
            "de home",
            DE_HOME.is_dir(),
            str(DE_HOME),
        )
    )
    if venv_py.is_file():
        venv_py_detail = str(venv_py)
    elif venv_py.is_symlink():
        venv_py_detail = (
            f"{venv_py} → {os.readlink(venv_py)} (target deleted) — "
            f"rebuild ~/de/.venv"
        )
    else:
        venv_py_detail = "missing — recreate ~/de/.venv"
    checks.append(Check("de venv python", venv_py.is_file(), venv_py_detail))
    try:
        import typer  # noqa: F401
        import rich  # noqa: F401

        checks.append(Check("typer/rich", True, "importable"))
    except ImportError as e:
        checks.append(Check("typer/rich", False, str(e)))

    fzf = _which("fzf")
    checks.append(Check("fzf", bool(fzf), fzf or "brew install fzf"))

    # --- config ---
    checks.append(
        Check(
            "config.toml",
            CONFIG_PATH.is_file(),
            str(CONFIG_PATH) if CONFIG_PATH.is_file() else f"missing — copy example to {CONFIG_PATH}",
        )
    )
    for key, value in sorted(cfg.paths.items()):
        p = Path(value).expanduser()
        checks.append(Check(f"path:{key}", p.is_dir(), str(p)))

    # --- dbt local projects ---
    from .dbt_projects import configured_projects, default_project_name

    dbt_projs = configured_projects()
    default_dbt = default_project_name()
    if not dbt_projs:
        checks.append(
            Check(
                "dbt.projects",
                False,
                "none configured — add [dbt.projects] in config.toml",
            )
        )
    else:
        checks.append(
            Check(
                "dbt.projects",
                True,
                f"{len(dbt_projs)} configured; default={default_dbt or '(none)'}",
            )
        )
        for proj in dbt_projs:
            ok = proj.path.is_dir() and proj.has_project_yml
            detail = str(proj.path)
            if not proj.path.is_dir():
                detail += " (missing dir)"
            elif not proj.has_project_yml:
                detail += " (no dbt_project.yml)"
            checks.append(Check(f"dbt:{proj.name}", ok, detail))
            from .dbt_projects import describe_binary, resolve_dbt_binary

            binary = resolve_dbt_binary(proj, quiet=True) if ok else None
            if binary is None:
                detail = (
                    f"engine={proj.engine} — unresolved "
                    "(set binary= or install Core/.venv)"
                )
                from .dbt_projects import first_broken_binary

                broken = first_broken_binary(proj)
                if broken is not None:
                    path, problem = broken
                    detail = (
                        f"engine={proj.engine} — {path} exists but {problem}; "
                        f"rebuild the venv"
                    )
                checks.append(
                    Check(
                        f"dbt:{proj.name}:binary",
                        False,
                        detail,
                    )
                )
            else:
                checks.append(
                    Check(
                        f"dbt:{proj.name}:binary",
                        True,
                        f"engine={proj.engine} · {describe_binary(binary)}",
                    )
                )
            from .dbt_projects import has_cloud_credentials

            if proj.engine == "cloud" or (
                binary is not None and "cloud" in describe_binary(binary)
            ):
                checks.append(
                    Check(
                        f"dbt:{proj.name}:cloud_creds",
                        has_cloud_credentials(),
                        (
                            "~/.dbt/dbt_cloud.yml present"
                            if has_cloud_credentials()
                            else "missing ~/.dbt/dbt_cloud.yml — download from dbt Cloud CLI settings"
                        ),
                    )
                )

    # --- CLIs used by registry argv tools + common DE CLIs ---
    for cli in ("astro", "dbt", "curl", "jq", "sigma"):
        path = _which(cli)
        if cli == "sigma" and not path:
            home_sigma = Path.home() / ".sigma-cli" / "bin" / "sigma"
            if home_sigma.is_file():
                path = str(home_sigma)
        checks.append(
            Check(
                f"cli:{cli}",
                bool(path),
                path
                or (
                    "not on PATH — brew install sigmacomputing/tap/sigma-computing-cli"
                    if cli == "sigma"
                    else "not on PATH"
                ),
            )
        )

    sigma_env = Path.home() / ".config" / "sigma" / "env"
    checks.append(
        Check(
            "config:sigma/env",
            sigma_env.is_file(),
            str(sigma_env) if sigma_env.is_file() else f"missing — {sigma_env}",
        )
    )

    # --- secrets / auth (presence only; do not print secrets) ---
    ft_auth = bool(os.environ.get("FIVETRAN_BASIC_AUTH"))
    checks.append(
        Check(
            "env:FIVETRAN_BASIC_AUTH",
            ft_auth,
            "set" if ft_auth else "unset — export FIVETRAN_BASIC_AUTH for de fivetran",
        )
    )
    astro_token = bool(os.environ.get("ASTRO_API_TOKEN"))
    checks.append(
        Check(
            "env:ASTRO_API_TOKEN",
            True,  # optional; astro login also works
            "set (optional)" if astro_token else "unset (ok if `astro login` works)",
        )
    )

    if _which("astro"):
        code, out = _run(["astro", "deployment", "list"], timeout=25)
        # Non-zero often means not logged in; treat as soft fail.
        ok = code == 0
        detail = "astro deployment list ok" if ok else (out.splitlines() or ["astro not authenticated?"])[0][:120]
        checks.append(Check("astro auth", ok, detail))
    else:
        checks.append(Check("astro auth", False, "astro CLI missing"))

    # --- registered script targets exist ---
    for tool in TOOLS:
        if not tool.script:
            continue
        try:
            target = tool.resolve_command()[0]
            exists = Path(target).exists()
            checks.append(Check(f"tool:{tool.name}", exists, target))
        except KeyError as e:
            checks.append(Check(f"tool:{tool.name}", False, str(e)))

    table = Table(title="de doctor", show_header=True, header_style="bold cyan")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    hard_fail = 0
    for c in checks:
        # Optional / informational checks that shouldn't fail the run:
        soft = c.name in {"env:ASTRO_API_TOKEN", "cli:dbt", "cli:sigma", "config:sigma/env"}
        if c.ok:
            status = "[green]ok[/green]"
        elif soft:
            status = "[yellow]warn[/yellow]"
        else:
            status = "[red]FAIL[/red]"
            hard_fail += 1
        table.add_row(c.name, status, c.detail)

    console.print(table)
    if hard_fail:
        console.print(f"[red]{hard_fail} check(s) failed.[/red]")
        return 1
    console.print("[green]All required checks passed.[/green]")
    return 0
