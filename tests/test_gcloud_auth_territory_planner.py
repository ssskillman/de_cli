"""BI Apps menu entry for Territory Planner gcloud auth."""

from __future__ import annotations

import unittest
from pathlib import Path


class GcloudAuthTerritoryPlannerTests(unittest.TestCase):
    def test_registry_lists_the_bi_apps_command(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        tool = find_tool("gcloud-auth-territory-planner")
        assert tool is not None
        self.assertEqual(menu_group_for_tool(tool), "bi_apps")
        self.assertEqual(tool.menu_summary, "Gcloud Auth for Territory Planner")
        self.assertIn("territory-planner-auth", tool.aliases)
        script = Path(tool.script or "")
        self.assertTrue(script.is_file())
        text = script.read_text(encoding="utf-8")
        self.assertIn('ACCOUNT="shawn.skillman@iterable.com"', text)
        self.assertIn('gcloud auth login "$ACCOUNT"', text)
        self.assertIn('PROJECT="udi-uat-504519"', text)
        self.assertIn('gcloud config set project "$PROJECT"', text)
        self.assertIn("udi-territory-planner", text)
        self.assertIn("status.latestReadyRevisionName", text)


if __name__ == "__main__":
    unittest.main()
