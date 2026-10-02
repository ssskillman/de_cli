"""Load ~/.config/de/config.toml (and built-in defaults)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "de"
CONFIG_PATH = CONFIG_DIR / "config.toml"
DE_HOME = Path.home() / "de"

# Sensible defaults for this machine; overridden by config.toml.
_DEFAULT_PATHS = {
    "bi_astronomer": str(Path.home() / "github" / "bi-astronomer"),
    "bi_dbt": str(Path.home() / "github" / "bi-dbt"),
    "bi_react": str(Path.home() / "github" / "bi-react"),
    "daily_pulse": str(Path.home() / "github" / "daily_pulse"),
    "bi_applications": str(Path.home() / "github" / "bi-applications"),
}


@dataclass
class Config:
    paths: dict[str, str] = field(default_factory=dict)
    defaults: dict[str, str] = field(default_factory=dict)
    # Values are path strings or dicts: {path, engine?, binary?}
    dbt_projects: dict[str, object] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def path(self, key: str) -> Path | None:
        value = self.paths.get(key)
        if not value:
            return None
        return Path(value).expanduser()

    def require_path(self, key: str) -> Path:
        p = self.path(key)
        if p is None:
            raise KeyError(f"No path configured for '{key}' in {CONFIG_PATH}")
        return p

    def snowflake_local_user(self) -> str | None:
        """Human Snowflake login for browser SSO (not the Airflow service user in .env)."""
        env = os.environ.get("DE_SNOWFLAKE_LOCAL_USER", "").strip()
        if env:
            return env
        snow = self.raw.get("snowflake") or {}
        value = snow.get("local_user")
        if value is None:
            return None
        text = str(value).strip()
        return text or None


_CONFIG: Config | None = None


def load_config(*, reload: bool = False) -> Config:
    global _CONFIG
    if _CONFIG is not None and not reload:
        return _CONFIG

    paths = dict(_DEFAULT_PATHS)
    defaults: dict[str, str] = {"astro_env": "dev", "dbt_project": "bi-dbt"}
    dbt_projects: dict[str, object] = {}
    raw: dict = {}

    if CONFIG_PATH.is_file():
        with CONFIG_PATH.open("rb") as f:
            raw = tomllib.load(f)
        paths.update({k: str(v) for k, v in (raw.get("paths") or {}).items()})
        defaults.update({k: str(v) for k, v in (raw.get("defaults") or {}).items()})
        dbt_section = raw.get("dbt") or {}
        for k, v in (dbt_section.get("projects") or {}).items():
            # Keep string paths and nested tables (engine/binary) intact.
            dbt_projects[str(k)] = v
    # Allow env overrides for one-off experiments.
    for key in list(paths):
        env_key = f"DE_PATH_{key.upper()}"
        if os.environ.get(env_key):
            paths[key] = os.environ[env_key]

    # Default single-project map if user hasn't configured [dbt.projects] yet.
    if not dbt_projects and paths.get("bi_dbt"):
        dbt_projects = {"bi-dbt": paths["bi_dbt"]}

    _CONFIG = Config(
        paths=paths,
        defaults=defaults,
        dbt_projects=dbt_projects,
        raw=raw,
    )
    return _CONFIG
