"""Persist interactive menu preferences."""

from __future__ import annotations

from .config import CONFIG_DIR, load_config

LAST_GROUP_FILE = CONFIG_DIR / "menu_last_group"
RECENT_FILE = CONFIG_DIR / "menu_recent"
ALL_TOOLS_KEY = "__all__"
RECENT_KEY = "__recent__"
PINNED_KEY = "__pinned__"
GUIDE_KEY = "__guide__"
RECENT_MAX = 5

# Older menu saves used a single "astronomer" bucket.
_LEGACY_GROUP_KEYS: dict[str, str] = {"astronomer": "airflow"}


def normalize_menu_group_key(key: str) -> str:
    return _LEGACY_GROUP_KEYS.get(key, key)


def read_last_menu_group() -> str | None:
    if not LAST_GROUP_FILE.is_file():
        return None
    key = LAST_GROUP_FILE.read_text(encoding="utf-8").strip()
    if not key:
        return None
    return normalize_menu_group_key(key)


def write_last_menu_group(key: str) -> None:
    if key in {ALL_TOOLS_KEY, RECENT_KEY, PINNED_KEY, GUIDE_KEY}:
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LAST_GROUP_FILE.write_text(key, encoding="utf-8")


def read_pinned_tool_names() -> list[str]:
    menu = load_config().raw.get("menu") or {}
    pinned = menu.get("pinned")
    if not pinned:
        return []
    return [str(name).strip() for name in pinned if str(name).strip()]


def read_recent_tool_names() -> list[str]:
    if not RECENT_FILE.is_file():
        return []
    names = [
        line.strip()
        for line in RECENT_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return names[:RECENT_MAX]


def push_recent_tool(name: str) -> None:
    names = [name, *[n for n in read_recent_tool_names() if n != name]]
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    RECENT_FILE.write_text(
        "\n".join(names[:RECENT_MAX]) + "\n",
        encoding="utf-8",
    )
