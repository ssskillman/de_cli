"""UDI Catalog MCP smoke command in the Local tools menu."""

from __future__ import annotations

import stat
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "udi_catalog_mcp.sh"


class UdiCatalogMcpMenuTests(unittest.TestCase):
    def test_registry_is_a_local_read_only_smoke_check(self):
        from de_cli.menu_groups import menu_group_for_tool, menu_group_label
        from de_cli.registry import find_tool

        tool = find_tool("udi-catalog-mcp")
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool.menu_summary, "Catalog MCP health + ARR search")
        self.assertEqual(tool.category, "local")
        self.assertEqual(menu_group_for_tool(tool), "local")
        self.assertEqual(menu_group_label("local"), "Local")
        self.assertFalse(tool.menu_requires_confirm)
        self.assertIsNone(tool.argv)
        self.assertTrue(tool.script.endswith("scripts/udi_catalog_mcp.sh"))
        self.assertEqual(
            tool.resolve_command(),
            [str(Path.home() / "de" / "scripts" / "udi_catalog_mcp.sh")],
        )

    def test_script_checks_health_then_searches_arr(self):
        text = SCRIPT.read_text()
        self.assertTrue(SCRIPT.stat().st_mode & stat.S_IXUSR)
        self.assertIn("/health", text)
        self.assertIn('call_tool("search_kpis"', text)
        self.assertIn('"q": "ARR"', text)
        self.assertIn("IdentityTokenAuth", text)
