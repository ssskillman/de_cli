import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "sfdc_limits.py"
SPEC = importlib.util.spec_from_file_location("sfdc_limits", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class SfdcLimitsTests(unittest.TestCase):
    def test_rows_keep_max_and_remaining_and_sort_tightest_first(self):
        payload = {
            "DailyApiRequests": {"Max": 15000, "Remaining": 14998},
            "DataStorageMB": {"Max": 100, "Remaining": 12},
            "Ignored": "not-a-limit",
            "FileStorageMB": {"Max": 200, "Remaining": 24},
        }
        self.assertEqual(
            MODULE.limit_rows(payload),
            [
                ("DataStorageMB", 100, 12),
                ("FileStorageMB", 200, 24),
                ("DailyApiRequests", 15000, 14998),
            ],
        )

    def test_name_filter_is_a_case_insensitive_substring(self):
        rows = [("DailyApiRequests", 10, 9), ("DataStorageMB", 10, 1)]
        self.assertEqual(MODULE.filter_rows(rows, "api"), [("DailyApiRequests", 10, 9)])
        self.assertEqual(MODULE.filter_rows(rows, ""), rows)

    def test_table_shows_used_as_max_minus_remaining(self):
        text = MODULE.render_table([("DataStorageMB", 100, 12)])
        self.assertIn("DataStorageMB", text)
        self.assertIn("88", text)
        self.assertIn("12", text)

    def test_registry_uses_the_opp_formula_org_and_stays_in_sfdc(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        tool = find_tool("sf-limits")
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool.name, "sfdc-limits")
        self.assertEqual(menu_group_for_tool(tool), "sfdc")
        self.assertFalse(tool.menu_requires_confirm)
        self.assertEqual(tool.menu_hint, "")
        self.assertTrue(tool.argv[-1].endswith("scripts/sfdc_limits.py"))
