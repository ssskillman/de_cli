import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "opp_formula_refresh_menu.py"
SPEC = importlib.util.spec_from_file_location("opp_formula_refresh_menu", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)

OPEN = (
    "--open-only",
    "--write-snowflake",
    "--snowflake-auth",
    "externalbrowser",
)


class OppFormulaMenuTests(unittest.TestCase):
    def test_open_write_is_the_first_profile(self):
        self.assertEqual(MODULE.PROFILES[0][0], "open-write")
        self.assertEqual(MODULE.PROFILES[0][2], OPEN)

    def test_no_args_uses_the_picked_profile(self):
        args = MODULE.resolve_args([], pick=lambda: "open-write", custom_line=lambda: None)
        self.assertEqual(tuple(args), OPEN)

    def test_explicit_args_skip_the_picker(self):
        def boom():
            raise AssertionError("picker ran")

        forwarded = ["--full", "--write-snowflake", "--snowflake-auth", "externalbrowser"]
        self.assertEqual(
            MODULE.resolve_args(forwarded, pick=boom, custom_line=boom),
            forwarded,
        )

    def test_injected_snowflake_user_still_opens_the_picker(self):
        args = MODULE.resolve_args(
            ["--snowflake-user", "shawn"],
            pick=lambda: "open-smoke",
            custom_line=lambda: None,
        )
        self.assertEqual(
            args,
            ["--open-only", "--limit", "10", "--snowflake-user", "shawn"],
        )

    def test_cancel_does_not_build_a_command(self):
        self.assertIsNone(MODULE.resolve_args([], pick=lambda: None, custom_line=lambda: None))

    def test_empty_custom_flags_cancel(self):
        self.assertIsNone(
            MODULE.resolve_args([], pick=lambda: "custom", custom_line=lambda: "  ")
        )

    def test_registry_no_longer_forces_a_full_load(self):
        from de_cli.registry import find_tool

        tool = find_tool("opp-formula-refresh")
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool.default_args, ())
        self.assertFalse(tool.menu_requires_confirm)
        self.assertTrue(tool.script.endswith("scripts/opp_formula_refresh_menu.py"))
        self.assertIsNone(tool.root)
