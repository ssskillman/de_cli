"""Act account type is a Build license. Offer a move to Act+ for people not building."""

from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import sigma_act_build_to_act_plus as report
from sigma_license_audit import MemberAuditRecord, resolve_act_plus_account_type


def _person(**overrides) -> MemberAuditRecord:
    fields = {
        "email": "ada@iterable.com",
        "member_id": "m-ada",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "member_type": "Act",
        "license_type": "Build",
        "user_kind": "internal",
        "sigma_status": "active",
        "is_archived": False,
        "is_inactive": False,
        "is_departed": False,
        "is_paid": True,
        "utilization_status": "UNDERUTILIZED_BUILD",
        "recommendation": "Downgrade (View / Analyze)",
        "last_edit": "",
        "total_events": 12,
    }
    fields.update(overrides)
    return MemberAuditRecord(**fields)


class ActBuildToActPlusTests(unittest.TestCase):
    def test_only_act_build_users_who_are_not_building_are_listed(self):
        people = report.act_build_not_building(
            [
                _person(),
                _person(email="dormant@iterable.com", member_id="m-d", utilization_status="DORMANT", total_events=0),
                _person(email="builder@iterable.com", member_id="m-b", utilization_status="ACTIVE_BUILDER", last_edit="2026-10-01"),
                _person(email="named-build@iterable.com", member_id="m-n", member_type="build"),
                _person(email="actplus@iterable.com", member_id="m-p", member_type="Act+", license_type="Act", utilization_status="ACTIVE_ANALYST"),
                _person(email="gone@iterable.com", member_id="m-g", is_departed=True, utilization_status="DEPARTED"),
                _person(email="off@iterable.com", member_id="m-o", sigma_status="inactive", utilization_status="INACTIVE_PAID"),
            ]
        )
        self.assertEqual([person.email for person in people], ["ada@iterable.com", "dormant@iterable.com"])
        self.assertEqual(people[0].recommendation, "Move to Act+ (True Act)")

    def test_report_uses_the_utilization_table(self):
        text = report.report_text([_person()])
        self.assertIn(report.REPORT_TITLE, text)
        self.assertIn("Last Edit", text)
        self.assertIn("ada@iterable.com", text)
        self.assertIn("UNDERUTILIZED_BUILD", text)
        self.assertIn("Move to Act+ (True Act)", text)

    def test_decline_does_not_change_licenses(self):
        client = _Client()
        output = io.StringIO()
        with redirect_stdout(output):
            code = report.run_report(
                [_person(), _person(email="other@iterable.com", member_id="m-o", first_name="Other", last_name="Person")],
                client=client,
                pick_multi=lambda _entries, **_kwargs: ["ada@iterable.com"],
                input_fn=lambda _prompt: "n",
            )
        self.assertEqual(code, 0)
        self.assertEqual(client.updates, [])

    def test_picker_is_alphabetical_by_name(self):
        rows = report._choice_rows(
            [
                _person(first_name="Zara", last_name="Zed", email="z@iterable.com", member_id="m-z"),
                _person(first_name="Ada", last_name="Lovelace", email="a@iterable.com", member_id="m-a"),
            ]
        )
        self.assertEqual([email for email, _label in rows], ["a@iterable.com", "z@iterable.com"])

    def test_confirmation_lists_every_selected_person(self):
        client = _Client()
        output = io.StringIO()
        with redirect_stdout(output):
            report.run_report(
                [
                    _person(first_name="Zara", last_name="Zed", email="z@iterable.com", member_id="m-z"),
                    _person(),
                ],
                client=client,
                pick_multi=lambda _entries, **_kwargs: ["z@iterable.com", "ada@iterable.com"],
                input_fn=lambda _prompt: "n",
            )
        text = output.getvalue()
        self.assertLess(text.index("Ada Lovelace"), text.index("Zara Zed"))
        self.assertIn("Act (Build license) → Act+ (True Act)", text)
        self.assertIn("ada@iterable.com", text)
        self.assertIn("z@iterable.com", text)
        self.assertEqual(client.updates, [])

    def test_confirm_moves_only_the_people_picked(self):
        client = _Client()
        prompts = []
        report.run_report(
            [_person(), _person(email="other@iterable.com", member_id="m-o")],
            client=client,
            pick_multi=lambda _entries, **_kwargs: ["ada@iterable.com"],
            input_fn=lambda prompt: prompts.append(prompt) or "y",
            act_plus="Act+",
        )
        self.assertEqual(client.updates, [("m-ada", "Act+")])
        self.assertIn("Act+", prompts[-1])
        self.assertIn("1", prompts[-1])

    def test_renamed_true_act_type_is_used_when_act_plus_is_gone(self):
        self.assertEqual(
            resolve_act_plus_account_type(
                {"Act": "Build", "Act (True)": "Act", "View": "View", "build": "Build"}
            ),
            "Act (True)",
        )

    def test_act_plus_name_still_wins_when_it_exists(self):
        self.assertEqual(
            resolve_act_plus_account_type({"Act+": "Act", "Act (True)": "Act", "Act": "Build"}),
            "Act+",
        )

    def test_registry_lists_the_sigma_command(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        tool = find_tool("sigma-act-build-to-act-plus")
        assert tool is not None
        self.assertEqual(menu_group_for_tool(tool), "sigma")
        self.assertEqual(tool.menu_summary, report.REPORT_TITLE)
        self.assertTrue(tool.menu_requires_confirm)


class _Client:
    def __init__(self):
        self.updates: list[tuple[str, str]] = []

    def update_member_type(self, member_id, member_type):
        self.updates.append((member_id, member_type))


if __name__ == "__main__":
    unittest.main()
