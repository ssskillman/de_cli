"""Court Reserve menu commands and next-launch math."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT = Path(__file__).parents[1] / "scripts" / "court_reserve_next.py"
SPEC = importlib.util.spec_from_file_location("court_reserve_next", SCRIPT)
NEXT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(NEXT)

EASTERN = ZoneInfo("America/New_York")
WEEKEND = [
    {"Hour": 7, "Minute": 30, "Weekday": 5},
    {"Hour": 7, "Minute": 30, "Weekday": 6},
]


class CourtReserveStatusMenuTests(unittest.TestCase):
    def test_registry_is_personal_and_read_only(self):
        from de_cli.menu_groups import menu_group_for_tool, menu_group_label
        from de_cli.registry import find_tool

        tool = find_tool("cr-status")
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool.name, "court-reserve-status")
        self.assertEqual(tool.menu_summary, "Court Reserve Status Check")
        self.assertEqual(tool.category, "personal")
        self.assertEqual(menu_group_for_tool(tool), "personal")
        self.assertEqual(menu_group_label("personal"), "Personal")
        self.assertFalse(tool.menu_requires_confirm)
        self.assertEqual(tool.menu_hint, "")
        self.assertTrue(tool.script.endswith("scripts/court_reserve_status.sh"))
        self.assertIsNone(tool.root)

    def test_normal_schedule_runs_the_live_booking_script(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        tool = find_tool("cr-schedule")
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool.name, "court-reserve-schedule")
        self.assertEqual(tool.menu_summary, "Court Reserve Normal Schedule")
        self.assertEqual(menu_group_for_tool(tool), "personal")
        self.assertTrue(tool.menu_requires_confirm)
        self.assertTrue(tool.script.endswith("bin/run_booking.sh"))

    def test_next_launch_is_personal_and_read_only(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        tool = find_tool("cr-next")
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool.name, "court-reserve-next")
        self.assertEqual(tool.menu_summary, "Court Reserve Next Launch")
        self.assertEqual(menu_group_for_tool(tool), "personal")
        self.assertFalse(tool.menu_requires_confirm)
        self.assertTrue(tool.script.endswith("scripts/court_reserve_next.py"))

    def test_live_status_is_personal_and_read_only(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        tool = find_tool("cr-live")
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool.name, "court-reserve-live")
        self.assertEqual(tool.menu_summary, "Court Reserve Live Status")
        self.assertEqual(menu_group_for_tool(tool), "personal")
        self.assertFalse(tool.menu_requires_confirm)
        self.assertTrue(tool.script.endswith("scripts/court_reserve_live.sh"))

    def test_friday_morning_before_730_launches_today(self):
        now = datetime(2026, 10, 23, 7, 2, tzinfo=EASTERN)
        nxt = min(NEXT.upcoming_runs(now, WEEKEND))
        self.assertEqual(nxt, datetime(2026, 10, 23, 7, 30, tzinfo=EASTERN))
        text = NEXT.render(now, WEEKEND)
        self.assertIn("Fri Oct 23, 2026 07:30:00 AM EDT", text)
        self.assertIn("Sat Oct 24, 2026", text)

    def test_after_friday_launch_the_next_run_is_saturday(self):
        now = datetime(2026, 10, 23, 8, 0, tzinfo=EASTERN)
        nxt = min(NEXT.upcoming_runs(now, WEEKEND))
        self.assertEqual(nxt, datetime(2026, 10, 24, 7, 30, tzinfo=EASTERN))

    def test_after_saturday_launch_the_next_run_is_next_friday(self):
        now = datetime(2026, 10, 24, 8, 0, tzinfo=EASTERN)
        nxt = min(NEXT.upcoming_runs(now, WEEKEND))
        self.assertEqual(nxt, datetime(2026, 10, 30, 7, 30, tzinfo=EASTERN))

    def test_a_single_schedule_dict_is_accepted(self):
        now = datetime(2026, 10, 23, 7, 2, tzinfo=EASTERN)
        nxt = min(NEXT.upcoming_runs(now, {"Hour": 7, "Minute": 30, "Weekday": 6}))
        self.assertEqual(nxt, datetime(2026, 10, 24, 7, 30, tzinfo=EASTERN))

    def test_no_future_runs_prints_the_empty_line(self):
        now = datetime(2026, 10, 23, 7, 2, tzinfo=EASTERN)
        self.assertEqual(NEXT.render(now, []), "No upcoming scheduled runs found.")


if __name__ == "__main__":
    unittest.main()
