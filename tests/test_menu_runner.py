import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from de_cli.menu_confirm import confirm_menu_run, slack_bot_token_in_env
    from de_cli.menu_pick import _fzf_pick
    from de_cli.registry import Tool
    from de_cli.runner import pause_after_tool
except ImportError:
    pause_after_tool = None
    _fzf_pick = None
    confirm_menu_run = None
    slack_bot_token_in_env = None
    Tool = None


class MenuRunnerTests(unittest.TestCase):
    def setUp(self):
        if pause_after_tool is None or _fzf_pick is None:
            raise unittest.SkipTest("de_cli requires .venv dependencies (rich, typer)")

    def test_pause_after_tool_enter_continues(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value=""):
            self.assertTrue(pause_after_tool())

    def test_pause_after_tool_q_exits(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="q"):
            self.assertFalse(pause_after_tool())

    def test_pause_after_tool_non_tty(self):
        with patch("sys.stdin.isatty", return_value=False):
            self.assertTrue(pause_after_tool())

    def test_slack_token_present_skips_confirm(self):
        tool = Tool(
            name="daily-pulse-smoke-est",
            description="smoke",
            script="scripts/run-local.sh",
            root="bi_applications",
            menu_requires_confirm=True,
        )
        with (
            patch("de_cli.menu_confirm.daily_pulse_smoke_ready", return_value=True),
            patch("builtins.input", side_effect=AssertionError("should not prompt")),
        ):
            self.assertTrue(confirm_menu_run(tool))

    def test_slack_token_missing_still_confirms(self):
        tool = Tool(
            name="daily-pulse-smoke-est",
            description="smoke",
            script="scripts/run-local.sh",
            root="bi_applications",
            menu_requires_confirm=True,
        )
        with (
            patch("de_cli.menu_confirm.daily_pulse_smoke_ready", return_value=False),
            patch("sys.stdin.isatty", return_value=True),
            patch("builtins.input", return_value="n"),
        ):
            self.assertFalse(confirm_menu_run(tool))

    def test_slack_bot_token_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("# comment\nexport SLACK_BOT_TOKEN='xoxb-1'\n")
            self.assertTrue(slack_bot_token_in_env(env_file))
            env_file.write_text("SLACK_BOT_TOKEN=\n")
            self.assertFalse(slack_bot_token_in_env(env_file))
            env_file.write_text("OTHER=1\n")
            self.assertFalse(slack_bot_token_in_env(env_file))

    def test_run_tool_uses_de_venv_python_for_py_scripts(self):
        from de_cli.runner import run_tool

        tool = Tool(
            name="opp-formula-refresh",
            description="test",
            script=str(Path.home() / "de" / "scripts" / "opp_formula_refresh_menu.py"),
        )
        with patch("de_cli.runner.subprocess.call", return_value=0) as mock_call:
            run_tool(tool, ["--help"])
        cmd = mock_call.call_args[0][0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("opp_formula_refresh_menu.py"))
        self.assertEqual(cmd[2], "--help")

    def test_fzf_pick_uses_no_mouse(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "key\tdisplay\n"
            result = _fzf_pick(
                ["key\tdisplay"],
                header="hdr",
                prompt="prompt> ",
                border_label="Fivetran",
            )
            self.assertEqual(result, "key")
            cmd = mock_run.call_args[0][0]
            self.assertIn("--no-mouse", cmd)
            self.assertIn("--info=inline-right", cmd)
            self.assertIn("--header-border=bottom", cmd)
            self.assertIn("--border-label= Fivetran ", cmd)

    def test_single_group_menu_aligns_actions_without_repeating_the_group(self):
        from de_cli.runner import tool_menu_view

        tools = [
            Tool(
                name="fivetran",
                description="run",
                menu_summary="Run a dbt selector",
                argv=("fivetran",),
            ),
            Tool(
                name="fivetran-running",
                description="live",
                menu_summary="Show what is running",
                argv=("fivetran-running",),
            ),
            Tool(
                name="fivetran-transformations",
                description="jobs",
                menu_summary="Run a saved transformation",
                argv=("fivetran-transformations",),
            ),
        ]
        header, rows = tool_menu_view(tools, show_group=False, group_key="fivetran")
        self.assertIn("command", header.splitlines()[1])
        self.assertNotIn("fivetran-running", "\n".join(rows))
        self.assertNotIn("fivetran-transformations", "\n".join(rows))
        action_at = {
            row.index("  Run") if "  Run" in row else row.index("  Show") for row in rows
        }
        self.assertEqual(len(action_at), 1)
        self.assertIn("transformations  Run a saved transformation", rows)
        self.assertTrue(any(row.startswith("fivetran ") for row in rows))

    def test_sigma_menu_drops_the_prefix_and_aligns_actions(self):
        from de_cli.runner import tool_menu_view

        tools = [
            Tool(name="sigma", description="cli", menu_summary="Mint a token, then open the CLI", argv=("sigma",)),
            Tool(name="sigma-gtm-team", description="gtm", menu_summary="GTM Team (Teri Hatfield)", argv=("gtm",)),
            Tool(
                name="sigma-swap-data-model-connection",
                description="swap",
                menu_summary="Swap a connection or transfer a model",
                argv=("swap",),
            ),
        ]
        _header, rows = tool_menu_view(tools, show_group=False, group_key="sigma")
        text = "\n".join(rows)
        self.assertIn("sigma  ", text)
        self.assertIn("gtm-team", text)
        self.assertIn("swap-data-model-connection", text)
        self.assertNotIn("sigma-", text)
        action_column = len("swap-data-model-connection") + 2
        self.assertTrue(all(row[action_column - 2 : action_column] == "  " for row in rows))
        self.assertTrue(all(row[action_column] != " " for row in rows))


if __name__ == "__main__":
    unittest.main()
