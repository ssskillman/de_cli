"""Pick people and move them to another Sigma account type."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import sigma_update_license as update


def _member(**overrides) -> dict:
    row = {
        "memberId": "m-ada",
        "email": "ada@iterable.com",
        "firstName": "Ada",
        "lastName": "Lovelace",
        "memberType": "Act",
        "isArchived": False,
        "isInactive": False,
        "userKind": "internal",
    }
    row.update(overrides)
    return row


TYPES = {"Act": "Build", "Act (True)": "Act", "View": "View", "build": "Build"}


class SigmaUpdateLicenseTests(unittest.TestCase):
    def test_account_type_label_shows_the_license_in_parentheses(self):
        self.assertEqual(update.account_type_label("Act (True)", "Act"), "Act (True) (Act)")
        self.assertEqual(update.account_type_label("Act", "Build"), "Act (Build)")

    def test_people_choices_are_alphabetical_and_skip_inactive(self):
        members = [
            _member(memberId="m-zoe", email="zoe@iterable.com", firstName="Zoe", lastName="Ng", memberType="View"),
            _member(),
            _member(memberId="m-old", email="old@iterable.com", firstName="Old", lastName="One", isArchived=True),
            _member(memberId="m-embed", email="bot@iterable.com", firstName="Bot", lastName="User", userKind="embed"),
        ]
        choices = update.people_choices(members, TYPES)
        self.assertEqual([key for key, _label in choices], ["m-ada", "m-zoe"])
        ada = choices[0][1]
        self.assertIn("Ada Lovelace", ada)
        self.assertIn("ada@iterable.com", ada)
        self.assertIn("Act (Build)", ada)

    def test_account_type_choices_show_license_in_parentheses(self):
        labels = dict(update.account_type_choices(TYPES))
        self.assertEqual(labels["Act (True)"], "Act (True) (Act)")
        self.assertEqual(labels["Act"], "Act (Build)")
        self.assertEqual(list(labels), ["Act", "Act (True)", "build", "View"])

    def test_proposal_names_every_person_and_the_target(self):
        text = update.proposal(
            [_member(), _member(memberId="m-zoe", email="zoe@iterable.com", firstName="Zoe", lastName="Ng", memberType="View")],
            "Act (True)",
            TYPES,
        )
        self.assertIn("Act (True) (Act)", text)
        self.assertIn("Ada Lovelace", text)
        self.assertIn("ada@iterable.com", text)
        self.assertIn("Act (Build)", text)
        self.assertIn("Zoe Ng", text)
        self.assertIn("View (View)", text)

    def test_confirm_no_does_not_change_anyone(self):
        client = _Client()
        output = io.StringIO()
        with redirect_stdout(output):
            code = update.run(
                client,
                pick_multi=lambda _entries, _header: ["m-ada"],
                pick=lambda _entries, _header: "Act (True)",
                input_fn=lambda _prompt: "n",
                members=[_member()],
                type_to_tier=TYPES,
            )
        self.assertEqual(code, 0)
        self.assertEqual(client.updates, [])
        self.assertIn("Ada Lovelace", output.getvalue())

    def test_confirm_yes_sets_the_account_type_name(self):
        client = _Client()
        with redirect_stdout(io.StringIO()):
            code = update.run(
                client,
                pick_multi=lambda _entries, _header: ["m-ada", "m-same"],
                pick=lambda _entries, _header: "View",
                input_fn=lambda _prompt: "y",
                members=[
                    _member(),
                    _member(memberId="m-same", email="same@iterable.com", firstName="Sam", lastName="E", memberType="View"),
                ],
                type_to_tier=TYPES,
            )
        self.assertEqual(code, 0)
        self.assertEqual(client.updates, [("m-ada", "View")])

    def test_registry_lists_the_sigma_command(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        tool = find_tool("sigma-update-license")
        assert tool is not None
        self.assertEqual(menu_group_for_tool(tool), "sigma")
        self.assertEqual(tool.menu_summary, "Update license")
        self.assertTrue(tool.menu_requires_confirm)


class _Client:
    def __init__(self):
        self.updates: list[tuple[str, str]] = []

    def update_member_type(self, member_id, member_type):
        self.updates.append((member_id, member_type))


if __name__ == "__main__":
    unittest.main()
