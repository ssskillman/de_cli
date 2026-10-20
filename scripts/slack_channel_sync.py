#!/usr/bin/env python3
"""
Incremental Slack channel export for FAQ analysis.

Uses conversations.history + conversations.replies (not search API).

Auth: ~/.config/de/slack.env — SLACK_BOT_TOKEN (or SLACK_USER_TOKEN).
Bot/app must be invited to the channel.

Usage:
  python3 scripts/slack_channel_sync.py --channel C01V64XA4AK --full
  python3 scripts/slack_channel_sync.py --channel C01V64XA4AK
  python3 scripts/slack_channel_sync.py --verify-auth --channel C01V64XA4AK
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from slack_faq_lib import (
    default_out_dir,
    default_state_dir,
    message_row,
    read_messages_csv,
    slack_api,
    slack_token,
    ts_to_float,
    write_messages_csv,
)


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def verify_auth(channel_id: str) -> None:
    tok = slack_token()
    auth = slack_api("auth.test", token=tok)
    team = slack_api("team.info", token=tok)
    domain = (team.get("team") or {}).get("domain") or ""
    print(f"OK auth.test: team={auth.get('team')} user={auth.get('user')}")
    if domain:
        print(f"OK team.info: domain={domain}.slack.com")
    try:
        info = slack_api("conversations.info", token=tok, channel=channel_id)
        ch = info.get("channel") or {}
        print(
            f"OK conversations.info: name=#{ch.get('name')} "
            f"is_member={ch.get('is_member')}"
        )
        if not ch.get("is_member"):
            print(
                "WARNING: token identity is not in channel — "
                "invite the app/bot before sync.",
                flush=True,
            )
    except SystemExit as exc:
        print(exc, flush=True)


def fetch_history_page(
    channel_id: str,
    *,
    cursor: str | None,
    oldest: str | None,
    limit: int,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    params: dict[str, Any] = {"channel": channel_id, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    if oldest:
        params["oldest"] = oldest
    data = slack_api("conversations.history", **params)
    messages = data.get("messages") or []
    meta = data.get("response_metadata") or {}
    next_cursor = meta.get("next_cursor") or ""
    has_more = bool(data.get("has_more")) or bool(next_cursor)
    return messages, next_cursor or None, has_more


def fetch_all_history(
    channel_id: str,
    *,
    oldest: str | None,
    stop_before_ts: float | None,
) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    cursor: str | None = None
    page = 0
    while True:
        page += 1
        batch, cursor, has_more = fetch_history_page(
            channel_id, cursor=cursor, oldest=oldest, limit=200
        )
        if not batch:
            break
        for msg in batch:
            ts = ts_to_float(str(msg.get("ts") or ""))
            if stop_before_ts is not None and ts <= stop_before_ts:
                return collected
            collected.append(msg)
        if page % 5 == 0:
            print(f"  history page {page}, {len(collected)} messages so far", flush=True)
        if not has_more or not cursor:
            break
    return collected


def fetch_thread_replies(channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {
            "channel": channel_id,
            "ts": thread_ts,
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        data = slack_api("conversations.replies", **params)
        messages = data.get("messages") or []
        out.extend(messages)
        meta = data.get("response_metadata") or {}
        next_cursor = meta.get("next_cursor") or ""
        has_more = bool(data.get("has_more")) or bool(next_cursor)
        if not has_more or not next_cursor:
            break
        cursor = next_cursor
    return out


def thread_roots(messages: list[dict[str, Any]]) -> set[str]:
    roots: set[str] = set()
    for msg in messages:
        ts = str(msg.get("ts") or "")
        reply_count = int(msg.get("reply_count") or 0)
        thread_ts = str(msg.get("thread_ts") or "")
        if reply_count > 0 and ts:
            roots.add(ts)
        elif thread_ts:
            roots.add(thread_ts)
    return roots


def merge_messages(
    channel_id: str,
    history: list[dict[str, Any]],
    *,
    fetch_threads: bool,
) -> dict[tuple[str, str], dict[str, str]]:
    rows: dict[tuple[str, str], dict[str, str]] = {}
    for msg in history:
        ts = str(msg.get("ts") or "")
        if not ts:
            continue
        is_reply = bool(msg.get("thread_ts")) and msg.get("thread_ts") != ts
        row = message_row(msg, channel_id, is_thread_reply=is_reply)
        rows[(channel_id, ts)] = row

    if fetch_threads:
        roots = thread_roots(history)
        for i, root_ts in enumerate(sorted(roots), start=1):
            if i % 25 == 0:
                print(f"  thread fetch {i}/{len(roots)}", flush=True)
            try:
                replies = fetch_thread_replies(channel_id, root_ts)
            except SystemExit as exc:
                print(f"  skip thread {root_ts}: {exc}", flush=True)
                continue
            for msg in replies:
                ts = str(msg.get("ts") or "")
                if not ts:
                    continue
                is_reply = ts != root_ts
                row = message_row(msg, channel_id, is_thread_reply=is_reply)
                rows[(channel_id, ts)] = row
    return rows


def run_sync(
    channel_id: str,
    *,
    out_dir: Path,
    state_path: Path,
    full: bool,
    since: str | None,
    no_threads: bool,
) -> None:
    state = load_state(state_path)
    checkpoint = None if full else state.get("last_synced_ts")
    stop_before: float | None = None
    if since:
        oldest = since
        incremental = False
    elif full or not checkpoint:
        oldest = None
        incremental = False
    else:
        oldest = str(checkpoint)
        incremental = True

    messages_csv = out_dir / "messages.csv"
    existing = read_messages_csv(messages_csv)

    print(
        f"Sync channel={channel_id} mode="
        f"{'incremental' if incremental else 'full'} "
        f"oldest={oldest or '(all)'}",
        flush=True,
    )

    history = fetch_all_history(
        channel_id, oldest=oldest, stop_before_ts=stop_before
    )

    print(f"Fetched {len(history)} history messages", flush=True)
    new_rows = merge_messages(
        channel_id, history, fetch_threads=not no_threads
    )
    existing.update(new_rows)
    write_messages_csv(messages_csv, existing)

    if existing:
        max_row = max(existing.values(), key=lambda r: ts_to_float(r["message_ts"]))
        state["last_synced_ts"] = max_row["message_ts"]
    state["last_run_at"] = datetime.now(UTC).isoformat()
    state["message_count"] = len(existing)
    save_state(state_path, state)
    print(f"Wrote {len(existing)} rows to {messages_csv}", flush=True)
    print(f"Checkpoint last_synced_ts={state.get('last_synced_ts')}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Slack channel messages to CSV")
    parser.add_argument(
        "--channel",
        default="C01V64XA4AK",
        help="Slack channel ID (default: C01V64XA4AK)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Output directory (default: ~/.local/share/de/slack_faq/<channel>/)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="State directory (default: ~/.config/de/slack_faq/<channel>/)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Ignore checkpoint; paginate full history (still merges CSV)",
    )
    parser.add_argument(
        "--since",
        metavar="TS",
        help="Only fetch messages with ts >= TS (overrides checkpoint)",
    )
    parser.add_argument(
        "--no-threads",
        action="store_true",
        help="Skip conversations.replies (faster, incomplete threads)",
    )
    parser.add_argument(
        "--verify-auth",
        action="store_true",
        help="Run auth.test + conversations.info and exit",
    )
    args = parser.parse_args()

    channel_id = args.channel.strip()
    out_dir = args.out_dir or default_out_dir(channel_id)
    state_path = (args.state_dir or default_state_dir(channel_id)) / "state.json"

    if args.verify_auth:
        verify_auth(channel_id)
        return

    run_sync(
        channel_id,
        out_dir=out_dir,
        state_path=state_path,
        full=args.full,
        since=args.since,
        no_threads=args.no_threads,
    )


if __name__ == "__main__":
    main()
