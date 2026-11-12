"""SFDC_NetSuite Sync menu entries."""

from __future__ import annotations

import unittest
from pathlib import Path


class SfdcNetSuiteMenuTests(unittest.TestCase):
    def test_listener_smoke_uses_the_bi_astronomer_venv(self):
        from de_cli.menu_groups import menu_group_for_tool, menu_group_label
        from de_cli.registry import find_tool

        tool = find_tool("sfdc_netsuite-listener-smoke")
        assert tool is not None
        self.assertEqual(menu_group_for_tool(tool), "sfdc_netsuite")
        self.assertEqual(menu_group_label("sfdc_netsuite"), "SFDC_NetSuite Sync")
        self.assertEqual(tool.menu_summary, "Listener smoke")
        command = tool.resolve_command()
        self.assertTrue(command[0].endswith("/github/bi-astronomer/.venv/bin/python"))
        self.assertTrue(Path(command[0]).is_file())
        self.assertTrue(command[1].endswith("scripts/sf_contracts_netsuite_listener_smoke.py"))
        self.assertEqual(tool.default_args, ("--sf-org", "iterable-prod"))
        self.assertEqual(tool.resolve_cwd(), Path.home() / "github" / "bi-astronomer")

    def test_run_script_is_the_interactive_wrapper(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        tool = find_tool("sfdc_netsuite-run")
        assert tool is not None
        self.assertEqual(menu_group_for_tool(tool), "sfdc_netsuite")
        self.assertEqual(tool.menu_summary, "Run contract sync")
        script = Path(tool.resolve_command()[0])
        self.assertEqual(script.name, "run_sf_contracts_netsuite.sh")
        self.assertTrue(script.is_file())
        self.assertEqual(tool.resolve_cwd(), Path.home() / "github" / "bi-astronomer")

    def test_exception_report_asks_for_a_contract_then_verifies_netsuite(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        tool = find_tool("sfdc_netsuite-exception-report")
        assert tool is not None
        self.assertEqual(menu_group_for_tool(tool), "sfdc_netsuite")
        self.assertEqual(tool.menu_summary, "Exception report")
        self.assertEqual(tool.default_args, ("--exception-report",))
        script = Path(tool.resolve_command()[0])
        self.assertEqual(script.name, "run_sf_contracts_netsuite.sh")
        text = script.read_text(encoding="utf-8")
        self.assertIn("--exception-report", text)
        self.assertIn("--verify-ns", text)
        self.assertIn('read -r -p "Contract: "', text)

    def test_prod_full_send_asks_for_a_contract_then_writes(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        tool = find_tool("sfdc_netsuite-prod-full-send")
        assert tool is not None
        self.assertEqual(menu_group_for_tool(tool), "sfdc_netsuite")
        self.assertEqual(tool.menu_summary, "Prod full send")
        self.assertTrue(tool.menu_requires_confirm)
        self.assertEqual(tool.default_args, ("--prod-full-send",))
        script = Path(tool.resolve_command()[0])
        text = script.read_text(encoding="utf-8")
        self.assertIn("--prod-full-send", text)
        self.assertIn('read -r -p "Contract: "', text)


if __name__ == "__main__":
    unittest.main()
