# Codex handoff — adding tools to `de`

Personal DE ops toolkit. Thin Typer wrappers around CLIs and repo scripts.
Not a rewrite of Astro / dbt / Fivetran / Sigma. Menu + registry + subprocess.

Home: `~/de`  ·  Launcher: `~/bin/de` → `~/de/de`  ·  Config: `~/.config/de/config.toml` (outside git)

## Where to change what

| What | File | Notes |
|------|------|--------|
| Register a tool | `de_cli/registry.py` → `TOOLS` | **Only required code change.** `app.py` auto-registers every `Tool(...)`. |
| New repo path key | `~/.config/de/config.toml` `[paths]` **and** `config.example.toml` | Example is committed; live config is not. |
| Wrapper that only `de` owns | `~/de/<name>.sh` or `~/de/scripts/` | Keep wrappers tiny. |
| Script that belongs to a product repo | that repo’s `scripts/` | e.g. `~/github/bi-astronomer/scripts/` |
| Menu defaults | `Tool.default_args` | Applied **only** when the user passed zero args. Any explicit arg replaces them entirely. |
| README example line | `README.md` | Optional, one line. |
| Env / path health | `de_cli/doctor.py` | Only if you add a new required CLI, secret, or check. Most tools need nothing here. |

Do **not** edit `de_cli/app.py` for a normal tool. Do **not** put scripts under Airflow `dags/`. Do **not** scan folders — `de` never auto-discovers tools.

`dbt` is the exception: `runner.py` special-cases `tool.name == "dbt"` and `de_cli/dbt_projects.py` owns multi-project cwd + binary. Do not copy that pattern.

## Where to store the script

Pick **one**:

### A. Script already lives in another repo (usual)

Leave it there. Registry uses a **relative** `script=` plus `root=` matching `[paths]`.

```python
Tool(
    name="fivetran",
    description="Fivetran Individual Model Run — interactive dbt selector",
    script="scripts/fivetran_individual_model_run",
    root="bi_astronomer",          # → ~/.config/de/config.toml [paths].bi_astronomer
    aliases=("ft", "fivetran-model"),
    category="fivetran",
)
```

Examples: `opp-formula-refresh`, `customer-health-score`, `astro-dags`, `sf-opp-query`.

### B. Wrapper only `de` owns (auth, `.env`, venv)

Put a small launcher in `~/de/` (or `~/de/scripts/`). Use an **absolute** `script=`. Do not set `root=`.

```python
script=str(Path.home() / "de" / "daily_pulse.sh"),
cwd_root="daily_pulse",            # optional chdir to a [paths] key
default_args=("--hours", "24"),
```

Use this when the real command needs env the product CLI does not load itself. Pattern: `sigma_cli.sh`, `daily_pulse.sh`.

`daily_pulse.sh` sources `$HOME/github/daily_pulse/.env` then execs the venv. Without that, the pulse still “succeeds” but reports Jira/Airflow/Snowflake/Fivetran unavailable.

### C. Existing CLI on PATH

```python
argv=("astro", "deploy"),
cwd_root="bi_astronomer",
```

Exactly one of `argv=` or `script=` is required.

## Tool fields

```python
Tool(
    name="kebab-case-name",          # `de <name>`
    description="one line for fzf / --list",
    aliases=("short",),              # also registered as visible Typer commands
    category="airflow|dbt|fivetran|local|ops|salesforce|sigma|general",
    argv=("cmd", "sub"),             # XOR script=
    script="scripts/foo.sh",         # relative → needs root=; absolute → no root
    root="bi_astronomer",            # [paths] key
    cwd_root="bi_astronomer",        # optional chdir before exec
    default_args=("--full",),        # menu / bare `de <name>` only
)
```

Relative `script` without `root` raises at import.

## New path key checklist

1. `~/.config/de/config.toml`:
   ```toml
   [paths]
   my_repo = "/Users/shawn.skillman/github/my-repo"
   ```
2. Same key in `config.example.toml` (committed).
3. Optional: `_DEFAULT_PATHS` in `de_cli/config.py` so a missing config still resolves.
4. `de doctor` lists every `[paths]` entry — the directory must exist.

Current keys: `bi_astronomer`, `bi_dbt`, `bi_react`, `udi_dbt`, `daily_pulse`.

## Defaults and safety

- Menu pick with no args → `default_args` if set.
- `de foo --limit 10` → **does not** merge defaults; user args replace them.
- Do **not** put destructive flags (`--live-write`, `--confirm-prod`) in `default_args` unless Shawn asks.
- `opp-formula-refresh` has no `default_args`. No args opens an fzf load profile (open-only MERGE is first). Explicit args are forwarded and skip the picker.
- `customer-health-score` stays dry-run unless the user passes `--live-write`.
- `daily-pulse` always injects `--dry-run` in the wrapper (pulse CLI requires it; Slack post is Cursor Automation, not this CLI).
- `astro-users` (`~/de/astro_users.sh`) wraps `astro workspace user` and `astro deployment user`. No `default_args`. List is read-only. Add / update / remove in the menu require retyping the email. Workspace remove drops deployment access in the current workspace; it does not delete the Organization user.
- `sigma-move-workbooks` (`~/de/scripts/sigma_move_workbooks.py`) — paste workbook URL(s) or fzf folder browser, destination browser, owner keep/assign, Enter to confirm move. Non-interactive: `--workbook-url` or `--inode-id`, `--destination-folder-id`, `--owner keep`, `--execute --yes`.

## Verify (required)

```bash
de --list
de which <name>
de doctor
de <name> --help          # forwarded to the underlying script/CLI
```

Help is forwarded: Typer does not steal `-h` for registered tools.

If the script needs credentials, prove a **clean env** (`env -i HOME="$HOME" PATH=...`) still works, or document that the wrapper sources `.env`.

## Do not

- Invent Typer commands in `app.py` for registry tools.
- Commit `~/.config/de/config.toml`, `.env`, tokens, or `config/de.yaml`.
- Put DE ops wrappers in `bi-astronomer/dags/`.
- Point UDI at Homebrew dbt Cloud CLI — UDI is Core + `~/.dbt/profiles.yml`; `engine = "core"`.
- Add Cursor / AI attribution to commits or PRs.
- Assume `python` is on PATH; use a venv binary or `python3`.

## Layout

```text
~/de/
  de                         # bash launcher → de_cli
  HANDOFF.md                 # this file
  de_cli/registry.py         # ← add Tool(...) here
  de_cli/config.py           # [paths] defaults
  de_cli/runner.py           # subprocess + fzf menu; dbt special case
  de_cli/doctor.py
  de_cli/app.py              # do not touch for a new tool
  daily_pulse.sh             # de-owned wrappers
  sigma_cli.sh
  astro_users.sh
  scripts/                   # de-owned python helpers
  config.example.toml

~/.config/de/config.toml     # live paths (not in git)
~/github/bi-astronomer/scripts/   # product scripts
~/github/daily_pulse/             # pulse app (not a de wrapper)
```

## Copy-paste templates

**Product-repo script**

```python
    Tool(
        name="example-local",
        description="Run example from bi-astronomer",
        script="scripts/run-example.sh",
        root="bi_astronomer",
        aliases=("example",),
        category="local",
    ),
```

**de-owned wrapper**

```python
    Tool(
        name="example-pulse",
        description="Example digest (always --dry-run; no args = --hours 24).",
        script=str(Path.home() / "de" / "example_pulse.sh"),
        cwd_root="example_repo",
        aliases=("ep",),
        category="ops",
        default_args=("--hours", "24"),
    ),
```

**PATH CLI**

```python
    Tool(
        name="example-deploy",
        description="Run example deploy in bi-react",
        argv=("example-cli", "deploy"),
        cwd_root="bi_react",
        category="sigma",
    ),
```
