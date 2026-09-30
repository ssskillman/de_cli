# `de` — personal DE ops toolkit

Thin **Python (Typer) wrappers** around CLIs and repo scripts you already use.
Not a rewrite of Astro / dbt / Fivetran — a menu + registry + subprocess front door.

```text
~/de/                      # this project (personal; not in bi-astronomer git)
  de                       # launcher
  de_cli/
    registry.py            # ← add tools here (argv= CLI  or  script= file)
    config.py
    doctor.py
    runner.py
    app.py
  .venv/
  requirements.txt
  config.example.toml

~/.config/de/config.toml   # repo paths + defaults
~/bin/de → ~/de/de
```

```bash
de                         # fzf menu (↑/↓, type to filter; alpha by name)
de --list
de doctor                  # fzf, venv, paths, astro auth, Fivetran env
de astro-dags [args...]
de fivetran [args...]
de fivetran-transformations      # fzf: pick a transformation, Enter runs it
de transformations list          # name / type / status / destination
de sf-opp-query            # SF Opp SOQL — human/csv/json (+ --pin)
de opp-formula-refresh --limit 10 --open-only --write-snowflake
de netlify-prod-deploy     # cd salt_app && netlify_prod_deploy
de astro-deploy            # → (cd bi-astronomer && astro deploy)
de which fivetran
```

After any tool finishes, `de` asks **Return to de menu? [Y/n]** (Enter = yes).

Launching `de` prints an ANSI Compact “Iterable DE / -shawn” splash before the menu / command.

## Registry: CLI **or** script

```python
# Call an existing CLI (cwd from config path key):
Tool(
    name="astro-deploy",
    description="Run astro deploy from bi-astronomer",
    argv=("astro", "deploy"),
    cwd_root="bi_astronomer",
    category="airflow",
)

# Run a script under a configured repo:
Tool(
    name="fivetran",
    description="Fivetran Individual Model Run",
    script="scripts/fivetran_individual_model_run",
    root="bi_astronomer",
    aliases=("ft",),
    category="fivetran",
)
```

Edit `de_cli/registry.py`, then `de --list`.

## Config

```toml
[paths]
bi_astronomer = "/Users/you/github/bi-astronomer"
bi_dbt = "/Users/you/github/bi-dbt"
bi_react = "/Users/you/github/bi-react"
udi_dbt = "/Users/you/UDI/dbt"

[defaults]
astro_env = "dev"
dbt_project = "bi-dbt"   # default for `de dbt …`

[dbt.projects]
bi-dbt = "bi_dbt"        # path key or absolute path
udi = "udi_dbt"
```

Override a path for one shell: `DE_PATH_BI_DBT=/other/path de …`

### Multi-project dbt

`de dbt` always `cd`s into a configured project, then runs the **right binary**:

| Project | Typical engine | Binary |
|---------|----------------|--------|
| `udi` | **dbt Core** (`profiles.yml`) | `~/UDI/.venv/bin/dbt` |
| `bi-dbt` | **dbt Cloud CLI** or PATH | Homebrew `/opt/homebrew/bin/dbt` |

**UDI command that works:**

```bash
de dbt -p udi build --select stg_sfdc_account --full-refresh
de dbt -p udi build --select stg_sfdc_account
```

From the fzf menu (`de` → **dbt**): pick project **udi**, then a preset
(`udi-account-full-refresh` / `udi-account-incremental`) or **custom**.

**bi-dbt local Cloud CLI** needs `~/.dbt/dbt_cloud.yml` (download from dbt Cloud →
CLI / account settings). Without it, `de dbt -p bi-dbt …` fails. UDI Core does
**not** use that file (`profiles.yml` only). Until Cloud creds are installed, run
bi-dbt in the **dbt Cloud IDE**, or stick to `-p udi` locally.

```bash
de dbt projects                 # list projects + resolved Core/Cloud binary
de dbt run --select foo         # defaults.dbt_project
de dbt -p udi build --select stg_sfdc_account --full-refresh
de dbt -p udi run --select staging
de dbt --pick test              # fzf pick, then dbt test
de dbt -p bi-dbt --help         # underlying dbt help from that cwd
```

Config (`~/.config/de/config.toml`):

```toml
[dbt.projects]
bi-dbt = "bi_dbt"              # path key; engine=auto (PATH / nearby venv)

[dbt.projects.udi]
path = "udi_dbt"
engine = "core"                # core | cloud | auto
binary = "/Users/you/UDI/.venv/bin/dbt"
```

Add another project under `[dbt.projects]`. For Core projects, point `binary` at the
venv `dbt` (or rely on auto-discovery of `project/.venv` / `parent/.venv`).

**Do not** use Homebrew Cloud CLI for UDI locally — it looks for `dbt_cloud.yml`.
Cloud project switching (`dbt project use --id …`) is only for Cloud CLI projects:

```bash
de dbt -p bi-dbt project use --id <cloud-project-id>
```

## Setup

```bash
python3 -m venv ~/de/.venv
~/de/.venv/bin/pip install -r ~/de/requirements.txt
cp ~/de/config.example.toml ~/.config/de/config.toml   # if needed
ln -sfn ~/de/de ~/bin/de
de doctor
```

## Scope (POC)

- **In:** DE / data ops — Astro, Fivetran, local ingestion scripts, later dbt / Sigma as you register them.
- **Out for now:** Pi / FlightTracker, Netlify, Slack, git helpers.
- **Overnight failures:** covered by Slack today; not in `de` yet.

## bi-astronomer

Scripts stay in `bi-astronomer/scripts/`. This toolkit lives in `~/de` so it can stay personal / cross-repo without forcing a git commit.
