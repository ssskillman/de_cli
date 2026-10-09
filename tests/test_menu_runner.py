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

    def test_fzf_pick_uses_no_mouse(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "key\tdisplay\n"
            result = _fzf_pick(["key\tdisplay"], header="hdr", prompt="prompt> ")
            self.assertEqual(result, "key")
            cmd = mock_run.call_args[0][0]
            self.assertIn("--no-mouse", cmd)


if __name__ == "__main__":
    unittest.main()
