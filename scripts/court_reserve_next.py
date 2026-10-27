#!/usr/bin/env python3
"""Print the next CourtReserve LaunchAgent run. Read-only."""

from __future__ import annotations

import plistlib
from datetime import datetime, timedelta
from pathlib import Path

PLIST = Path.home() / "Library/LaunchAgents/com.shawn.courtbooking.plist"


def upcoming_runs(now: datetime, schedules: dict | list) -> list[datetime]:
    """Launch times in the next 8 days that are still ahead of `now`."""
    if isinstance(schedules, dict):
        schedules = [schedules]
    upcoming: list[datetime] = []
    for day_offset in range(8):
        day = now.date() + timedelta(days=day_offset)
        for schedule in schedules:
            # launchd: Sunday=0, Monday=1, ... Saturday=6
            launchd_weekday = (day.weekday() + 1) % 7
            if "Weekday" in schedule and schedule["Weekday"] != launchd_weekday:
                continue
            candidate = datetime(
                day.year,
                day.month,
                day.day,
                schedule.get("Hour", 0),
                schedule.get("Minute", 0),
                schedule.get("Second", 0),
                tzinfo=now.tzinfo,
            )
            if candidate > now:
                upcoming.append(candidate)
    return upcoming


def render(now: datetime, schedules: dict | list) -> str:
    upcoming = upcoming_runs(now, schedules)
    if not upcoming:
        return "No upcoming scheduled runs found."
    next_run = min(upcoming)
    lines = [
        "🎾 CourtReserve Scheduler",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Current time : {now:%a %b %d, %Y %I:%M:%S %p %Z}",
        f"Next launch  : {next_run:%a %b %d, %Y %I:%M:%S %p %Z}",
        f"Time until   : {str(next_run - now).split('.')[0]}",
        f"Booking date : {(next_run + timedelta(days=1)):%a %b %d, %Y}",
        "Submit time  : 7:45:00 AM (configured default)",
        "Court        : 2 preferred",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


def main() -> int:
    with PLIST.open("rb") as handle:
        config = plistlib.load(handle)
    now = datetime.now().astimezone()
    print(render(now, config.get("StartCalendarInterval", [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
