"""API user password expiration report."""

from __future__ import annotations

import importlib.util
import unittest
from datetime import UTC, datetime
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "sfdc_api_users.py"
SPEC = importlib.util.spec_from_file_location("sfdc_api_users", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)

NOW = datetime(2026, 10, 23, 14, 0, tzinfo=UTC)


def _user(name: str, expires: str | None, username: str = "a@iterable.com") -> dict:
    return {
        "Name": name,
        "Username": username,
        "Profile": {"Name": "Integration"},
        "PasswordExpirationDate": expires,
        "LastLoginDate": "2026-10-23T12:00:00.000+0000",
    }


class SfdcApiUsersTests(unittest.TestCase):
    def test_past_password_expiration_is_expired(self):
        rows = MODULE.user_rows(
            [
                _user("Still good", "2027-01-01T00:00:00.000+0000", "good@iterable.com"),
                _user("Old password", "2022-10-03T20:09:21.000+0000", "old@iterable.com"),
                _user("No date", None, "none@iterable.com"),
            ],
            NOW,
        )
        self.assertEqual([row["status"] for row in rows], ["expired", "active", "none"])
        self.assertEqual(rows[0]["user"], "Old password")
        self.assertIn("2022-10-03", rows[0]["expires"])

    def test_expired_table_is_only_the_expired_rows(self):
        rows = MODULE.user_rows(
            [
                _user("Old password", "2022-10-03T20:09:21.000+0000"),
                _user("Still good", "2027-01-01T00:00:00.000+0000"),
            ],
            NOW,
        )
        expired = [row for row in rows if row["status"] == "expired"]
        text = MODULE.render_table(expired)
        self.assertIn("Old password", text)
        self.assertNotIn("Still good", text)
        self.assertIn("Password expires", text)

    def test_query_does_not_ask_for_secrets(self):
        self.assertIn("PasswordExpirationDate", MODULE.USER_QUERY)
        self.assertIn("PermissionsApiUserOnly", MODULE.USER_QUERY)
        self.assertNotIn("AccessToken", MODULE.USER_QUERY)
        self.assertNotIn("Password", MODULE.USER_QUERY.replace("PasswordExpirationDate", ""))

    def test_registry_is_read_only_sfdc(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        tool = find_tool("sf-api-users")
        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool.name, "sfdc-api-users")
        self.assertEqual(tool.menu_summary, "API users and password expiration")
        self.assertEqual(menu_group_for_tool(tool), "sfdc")
        self.assertFalse(tool.menu_requires_confirm)
        self.assertTrue(tool.argv[-1].endswith("scripts/sfdc_api_users.py"))


if __name__ == "__main__":
    unittest.main()
