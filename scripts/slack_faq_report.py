#!/usr/bin/env python3
"""
Build FAQ frequency reports from slack_channel_sync CSV output.

Usage:
  python3 scripts/slack_faq_report.py
  python3 scripts/slack_faq_report.py --in-dir ~/.local/share/de/slack_faq/C01V64XA4AK
  python3 scripts/slack_faq_report.py --merge-similar
"""

from __future__ import annotations

import argparse
import csv
import difflib
import os
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from slack_faq_lib import (
    SKIP_SUBTYPES,
    default_out_dir,
    is_question,
    load_slack_env,
    normalize_question_text,
    permalink,
    read_messages_csv,
    slack_api,
    slack_token,
    ts_to_float,
)


def iso_week(ts: str) -> str:
    dt = datetime.fromtimestamp(ts_to_float(ts), tz=UTC)
    year, week, _ = dt.isocalendar()
    return f"{year}-W{week:02d}"


def team_domain() -> str:
    load_slack_env()
    env_dom = os.environ.get("SLACK_TEAM_DOMAIN", "").strip()
    if env_dom:
        return env_dom.removesuffix(".slack.com")
    try:
        tok = slack_token()
        data = slack_api("team.info", token=tok)
        return str((data.get("team") or {}).get("domain") or "slack")
    except SystemExit:
        return os.environ.get("SLACK_TEAM_DOMAIN", "slack").strip() or "slack"


def merge_similar_buckets(
    buckets: dict[str, list[dict[str, str]]],
    threshold: float,
) -> dict[str, list[dict[str, str]]]:
    keys = sorted(buckets.keys(), key=lambda k: -len(buckets[k]))
    merged: dict[str, list[dict[str, str]]] = {}
    canonical: list[str] = []

    for key in keys:
        rows = buckets[key]
        if not key:
            continue
        target = None
        for can in canonical:
            if difflib.SequenceMatcher(None, key, can).ratio() >= threshold:
                target = can
                break
        if target is None:
            canonical.append(key)
            merged[key] = list(rows)
        else:
            merged[target].extend(rows)
    return merged


def build_reports(
    in_dir: Path,
    *,
    channel_id: str,
    merge_similar: bool,
    similarity: float,
    include_non_questions: bool,
) -> None:
    messages_path = in_dir / "messages.csv"
    rows = read_messages_csv(messages_path)
    if not rows:
        raise SystemExit(f"No messages at {messages_path} — run slack_channel_sync first.")

    domain = team_domain()
    question_rows: list[dict[str, str]] = []
    non_question_sample: list[dict[str, str]] = []

    for row in rows.values():
        text = row.get("text", "").strip()
        subtype = row.get("subtype", "")
        if not text or subtype in SKIP_SUBTYPES:
            continue
        if subtype == "bot_message" and not is_question(text):
            continue
        if is_question(text):
            norm = normalize_question_text(text)
            if not norm:
                continue
            enriched = dict(row)
            enriched["normalized_text"] = norm
            enriched["iso_week"] = iso_week(row["message_ts"])
            enriched["permalink"] = permalink(
                domain, row["channel_id"] or channel_id, row["message_ts"]
            )
            question_rows.append(enriched)
        elif include_non_questions and len(non_question_sample) < 500:
            if subtype not in ("bot_message",) and len(text) > 20:
                non_question_sample.append(row)

    buckets: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in question_rows:
        buckets[row["normalized_text"]].append(row)

    if merge_similar:
        buckets = merge_similar_buckets(buckets, similarity)

    faq_rows: list[dict[str, str]] = []
    for norm, items in buckets.items():
        items.sort(key=lambda r: r["message_ts"])
        first = items[0]
        last = items[-1]
        example = max(items, key=lambda r: len(r.get("text", "")))
        faq_rows.append(
            {
                "count": str(len(items)),
                "normalized_text": norm,
                "example_text": example.get("text", "")[:500],
                "first_ts": first["message_ts"],
                "last_ts": last["message_ts"],
                "sample_permalink": example.get("permalink", ""),
            }
        )

    faq_rows.sort(key=lambda r: (-int(r["count"]), r["normalized_text"]))

    week_counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in question_rows:
        norm = row["normalized_text"]
        week = row["iso_week"]
        week_counts[(norm, week)] += 1

    out_faq = in_dir / "faq_report.csv"
    out_week = in_dir / "faq_by_week.csv"
    out_sample = in_dir / "faq_unclassified_sample.csv"

    with out_faq.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=(
                "count",
                "normalized_text",
                "example_text",
                "first_ts",
                "last_ts",
                "sample_permalink",
            ),
        )
        writer.writeheader()
        writer.writerows(faq_rows)

    with out_week.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=("normalized_text", "iso_week", "count"),
        )
        writer.writeheader()
        for (norm, week), count in sorted(week_counts.items()):
            writer.writerow(
                {"normalized_text": norm, "iso_week": week, "count": str(count)}
            )

    with out_sample.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=("message_ts", "user_id", "text", "subtype", "thread_ts"),
        )
        writer.writeheader()
        for row in non_question_sample:
            writer.writerow(
                {
                    "message_ts": row.get("message_ts", ""),
                    "user_id": row.get("user_id", ""),
                    "text": row.get("text", "")[:500],
                    "subtype": row.get("subtype", ""),
                    "thread_ts": row.get("thread_ts", ""),
                }
            )

    print(f"Questions: {len(question_rows)} in {len(faq_rows)} buckets")
    print(f"Wrote {out_faq}")
    print(f"Wrote {out_week}")
    if include_non_questions:
        print(f"Wrote {out_sample} ({len(non_question_sample)} rows)")


def main() -> None:
    parser = argparse.ArgumentParser(description="FAQ report from synced Slack CSV")
    parser.add_argument("--channel", default="C01V64XA4AK")
    parser.add_argument("--in-dir", type=Path, default=None)
    parser.add_argument(
        "--merge-similar",
        action="store_true",
        help="Merge buckets with difflib ratio >= threshold",
    )
    parser.add_argument(
        "--similarity",
        type=float,
        default=0.85,
        help="Merge threshold when --merge-similar (default 0.85)",
    )
    parser.add_argument(
        "--include-non-questions",
        action="store_true",
        help="Write faq_unclassified_sample.csv",
    )
    args = parser.parse_args()

    channel_id = args.channel.strip()
    in_dir = args.in_dir or default_out_dir(channel_id)
    build_reports(
        in_dir,
        channel_id=channel_id,
        merge_similar=args.merge_similar,
        similarity=args.similarity,
        include_non_questions=args.include_non_questions,
    )


if __name__ == "__main__":
    main()
