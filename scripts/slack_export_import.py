#!/usr/bin/env python3
"""
Import Slack workspace export JSON into messages.csv for slack_faq_report.

Slack admin export layout (unzipped):
  channels.json
  users.json
  <channel-name>/YYYY-MM-DD.json   # arrays of message objects

Usage:
  python3 scripts/slack_export_import.py --export ~/Downloads/slack-export.zip --channel C01V64XA4AK
  python3 scripts/slack_export_import.py --export ~/Downloads/slack-export/ --channel C01V64XA4AK
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from slack_faq_lib import (
    default_out_dir,
    message_row,
    write_messages_csv,
)


def load_channels(export_root: Path) -> list[dict]:
    path = export_root / "channels.json"
    if not path.is_file():
        raise SystemExit(f"Missing {path} — is this a Slack workspace export?")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "channels" in data:
        return list(data["channels"])
    raise SystemExit(f"Unrecognized channels.json shape in {path}")


def resolve_channel_folder(
    export_root: Path, channels: list[dict], channel_id: str, channel_name: str | None
) -> tuple[str, Path]:
    if channel_name:
        folder = export_root / channel_name
        if folder.is_dir():
            ch = next((c for c in channels if c.get("name") == channel_name), None)
            cid = str((ch or {}).get("id") or channel_id)
            return cid, folder
        raise SystemExit(f"No folder {folder} in export")

    for ch in channels:
        if str(ch.get("id") or "") == channel_id:
            name = str(ch.get("name") or "")
            if not name:
                break
            folder = export_root / name
            if folder.is_dir():
                return channel_id, folder
            raise SystemExit(
                f"Channel id={channel_id} name={name} but folder {folder} not found"
            )
    raise SystemExit(
        f"Channel {channel_id} not in channels.json — pass --channel-name if needed"
    )


def iter_export_messages(channel_dir: Path) -> list[dict]:
    messages: list[dict] = []
    for path in sorted(channel_dir.glob("*.json")):
        if path.name in ("canvas.json",):
            continue
        try:
            batch = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(batch, list):
            messages.extend(batch)
        elif isinstance(batch, dict) and "messages" in batch:
            messages.extend(batch["messages"])
    return messages


def slack_msg_to_row(msg: dict, channel_id: str) -> dict[str, str] | None:
    if msg.get("type") not in (None, "message"):
        return None
    ts = str(msg.get("ts") or "")
    if not ts:
        return None
    thread_ts = str(msg.get("thread_ts") or "")
    is_reply = bool(thread_ts and thread_ts != ts)
    return message_row(msg, channel_id, is_thread_reply=is_reply)


def prepare_export_root(export_path: Path) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    if export_path.is_dir():
        return export_path, None
    if export_path.is_file() and zipfile.is_zipfile(export_path):
        tmp = tempfile.TemporaryDirectory(prefix="slack_export_")
        with zipfile.ZipFile(export_path) as zf:
            zf.extractall(tmp.name)
        root = Path(tmp.name)
        # Some zips wrap everything in a single top-level folder
        children = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
        if len(children) == 1 and (children[0] / "channels.json").is_file():
            return children[0], tmp
        return root, tmp
    raise SystemExit(f"Export path must be a directory or zip: {export_path}")


def run_import(
    export_path: Path,
    channel_id: str,
    channel_name: str | None,
    out_dir: Path,
) -> None:
    root, tmp = prepare_export_root(export_path)
    try:
        channels = load_channels(root)
        cid, channel_dir = resolve_channel_folder(root, channels, channel_id, channel_name)
        raw = iter_export_messages(channel_dir)
        rows: dict[tuple[str, str], dict[str, str]] = {}
        for msg in raw:
            row = slack_msg_to_row(msg, cid)
            if row:
                rows[(cid, row["message_ts"])] = row
        if not rows:
            raise SystemExit(f"No messages parsed under {channel_dir}")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "messages.csv"
        write_messages_csv(out_path, rows)
        print(f"Imported {len(rows)} messages → {out_path}")
    finally:
        if tmp:
            tmp.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import Slack workspace export into FAQ messages.csv"
    )
    parser.add_argument(
        "--export",
        type=Path,
        required=True,
        help="Path to slack export .zip or unzipped folder",
    )
    parser.add_argument("--channel", default="C01V64XA4AK")
    parser.add_argument(
        "--channel-name",
        help="Folder name in export if channels.json lookup fails",
    )
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    channel_id = args.channel.strip()
    out_dir = args.out_dir or default_out_dir(channel_id)
    run_import(args.export, channel_id, args.channel_name, out_dir)


if __name__ == "__main__":
    main()
