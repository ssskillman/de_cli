import unittest
from unittest.mock import patch

try:
    from de_cli.runner import pause_after_tool
    from de_cli.menu_pick import _fzf_pick
except ImportError:
    pause_after_tool = None
    _fzf_pick = None


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
