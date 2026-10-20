"""Shared helpers for Slack FAQ export (stdlib only)."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_SLACK_ENV = Path.home() / ".config" / "de" / "slack.env"
SLACK_API = "https://slack.com/api/"

MESSAGE_COLUMNS = (
    "channel_id",
    "message_ts",
    "thread_ts",
    "is_thread_reply",
    "user_id",
    "text",
    "subtype",
    "reply_count",
    "reaction_count",
)


def load_slack_env(path: Path | None = None) -> None:
    env_file = path or DEFAULT_SLACK_ENV
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def slack_token() -> str:
    load_slack_env()
    for key in ("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN", "SLACK_TOKEN"):
        val = os.environ.get(key, "").strip()
        if val:
            return val
    raise SystemExit(
        "Missing Slack token. Set SLACK_BOT_TOKEN in "
        f"{DEFAULT_SLACK_ENV} (see config.example.toml [slack_faq])."
    )


def slack_api(
    method: str,
    *,
    token: str | None = None,
    retries: int = 8,
    **params: Any,
) -> dict[str, Any]:
    tok = token or slack_token()
    body = {k: v for k, v in params.items() if v is not None}
    data = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(
        SLACK_API + method,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8")
                retry_after = resp.headers.get("Retry-After")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt + 1 < retries:
                wait = int(exc.headers.get("Retry-After", "5"))
                time.sleep(wait)
                continue
            raise SystemExit(f"Slack HTTP {exc.code}: {exc.reason}") from exc

        payload = json.loads(raw)
        if payload.get("ok"):
            return payload

        err = payload.get("error", "unknown_error")
        if err == "ratelimited" and attempt + 1 < retries:
            wait = int(retry_after or payload.get("retry_after") or 5)
            time.sleep(wait)
            continue
        raise SystemExit(f"Slack API {method} failed: {err}")

    raise SystemExit(f"Slack API {method}: exhausted retries")


def default_state_dir(channel_id: str) -> Path:
    return Path.home() / ".config" / "de" / "slack_faq" / channel_id


def default_out_dir(channel_id: str) -> Path:
    return Path.home() / ".local" / "share" / "de" / "slack_faq" / channel_id


def message_row(msg: dict[str, Any], channel_id: str, *, is_thread_reply: bool) -> dict[str, str]:
    reactions = msg.get("reactions") or []
    reaction_count = sum(int(r.get("count", 0)) for r in reactions)
    thread_ts = msg.get("thread_ts") or ""
    if not thread_ts and int(msg.get("reply_count") or 0) > 0:
        thread_ts = msg.get("ts") or ""
    return {
        "channel_id": channel_id,
        "message_ts": str(msg.get("ts") or ""),
        "thread_ts": str(thread_ts),
        "is_thread_reply": "1" if is_thread_reply else "0",
        "user_id": str(msg.get("user") or msg.get("bot_id") or ""),
        "text": str(msg.get("text") or ""),
        "subtype": str(msg.get("subtype") or ""),
        "reply_count": str(int(msg.get("reply_count") or 0)),
        "reaction_count": str(reaction_count),
    }


def read_messages_csv(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    if not path.is_file():
        return {}
    import csv

    rows: dict[tuple[str, str], dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            key = (row.get("channel_id", ""), row.get("message_ts", ""))
            if key[0] and key[1]:
                rows[key] = {k: row.get(k, "") for k in MESSAGE_COLUMNS}
    return rows


def write_messages_csv(path: Path, rows: dict[tuple[str, str], dict[str, str]]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_rows = sorted(rows.values(), key=lambda r: (r["channel_id"], r["message_ts"]))
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=MESSAGE_COLUMNS)
        writer.writeheader()
        writer.writerows(sorted_rows)


def ts_to_float(ts: str) -> float:
    try:
        return float(ts)
    except ValueError:
        return 0.0


def permalink(domain: str, channel_id: str, message_ts: str) -> str:
    dom = domain.strip().removesuffix(".slack.com")
    p_ts = message_ts.replace(".", "")
    return f"https://{dom}.slack.com/archives/{channel_id}/p{p_ts}"


_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_CHANNEL_RE = re.compile(r"<#[A-Z0-9]+\|[^>]+>|<#[A-Z0-9]+>")
_URL_RE = re.compile(r"<https?://[^>|]+(?:\|[^>]+)?>|https?://\S+")
_EMOJI_RE = re.compile(r":[a-z0-9_+-]+:")
_WS_RE = re.compile(r"\s+")


def normalize_question_text(text: str) -> str:
    t = text.strip().lower()
    t = _MENTION_RE.sub("", t)
    t = _CHANNEL_RE.sub("", t)
    t = _URL_RE.sub("", t)
    t = _EMOJI_RE.sub("", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


_QUESTION_START = re.compile(
    r"^\s*(how|what|when|where|why|who|which|can|could|would|should|does|do|did|is|are|was|were|anyone|anybody|help)\b",
    re.IGNORECASE,
)


def is_question(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if "?" in t:
        return True
    return bool(_QUESTION_START.search(t))


SKIP_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "group_join",
        "group_leave",
        "channel_archive",
        "channel_unarchive",
    }
)
