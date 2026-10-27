"""Sigma team create and team license changes."""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

import sigma_team_admin as admin
from sigma_license_audit import ReportingWorker


def _person(email: str, name: str, manager: str) -> ReportingWorker:
    return ReportingWorker(
        email=email,
        full_name=name,
        depth=1,
        manager_email=manager,
        job_title="",
    )


SAM = "sam.allen@iterable.com"
TERI = "teri.hatfield@iterable.com"
PAT = "pat@iterable.com"
KIM = "kim@iterable.com"
JO = "jo@iterable.com"
ALEX = "alex@iterable.com"

WORKERS = [
    _person(TERI, "Teri Hatfield", SAM),
    _person(ALEX, "Alex", SAM),
    _person(PAT, "Pat", TERI),
    _person(KIM, "Kim", TERI),
    _person(JO, "Jo", KIM),
]


def _no_multi(entries, header):
    return []


class _Client:
    def __init__(self):
        self.calls = []

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/v2/teams":
            return {"entries": [{"teamId": "gtm", "name": "Go-To-Market (GTM)"}]}
        if method == "POST":
            return {"teamId": "new-id"}
        return {}

    def load_members(self):
        return [{"memberId": "m-alex", "email": ALEX}]


class _LicenseClient:
    def __init__(self):
        self.updates: list[tuple[str, str]] = []

    def request(self, method, path, body=None, params=None):
        if path == "/v2/teams":
            return {"entries": [{"teamId": "gtm", "name": "Go-To-Market (GTM)"}]}
        if path.endswith("/members"):
            return {"entries": [{"userId": "1"}, {"userId": "2"}, {"userId": "3"}]}
        return {}

    def load_account_types_map(self):
        return {"View": "View", "Act+": "Act", "Build": "Build"}

    def load_members(self):
        return [
            {"memberId": "1", "email": "a@iterable.com", "memberType": "View"},
            {"memberId": "2", "email": "b@iterable.com", "memberType": "View"},
            {"memberId": "3", "email": "c@iterable.com", "memberType": "Act+"},
        ]

    def update_member_type(self, member_id, member_type):
        self.updates.append((member_id, member_type))


class SigmaTeamAdminTests(unittest.TestCase):
    def test_whole_team_includes_the_leader_and_everyone_below(self):
        self.assertEqual(
            admin.whole_team_emails(WORKERS, TERI),
            sorted([TERI, PAT, KIM, JO]),
        )

    def test_selecting_a_leader_can_move_her_whole_team(self):
        answers = iter([TERI, "whole", "__done__"])

        def pick(entries, _header):
            return next(answers)

        emails = admin.gather_member_emails(
            WORKERS,
            [WORKERS[0], WORKERS[1]],
            pick=pick,
            pick_multi=lambda entries, header: [],
        )
        self.assertEqual(emails, sorted([TERI, PAT, KIM, JO]))

    def test_one_level_down_excludes_a_tree_then_confirms_the_team(self):
        answers = iter([TERI, "teams", "__done__"])
        actions = iter([("exclude", KIM), ("done", None)])
        headers = []

        def pick_level(_entries, **kwargs):
            headers.append(kwargs["header"])
            return next(actions)

        prompts = []
        output = io.StringIO()
        with redirect_stdout(output):
            emails = admin.gather_member_emails(
                WORKERS,
                [WORKERS[0], WORKERS[1]],
                pick=lambda _entries, _header: next(answers),
                pick_level=pick_level,
                team_name="Go-To-Market (GTM)",
                input_fn=lambda prompt: prompts.append(prompt) or "y",
            )
        self.assertEqual(emails, [PAT])
        self.assertIn("Go-To-Market (GTM)", headers[0])
        self.assertIn("Ctrl-X=exclude", headers[0])
        self.assertIn("Ctrl-Y=include", headers[0])
        self.assertIn("Add 1 person to Go-To-Market (GTM)?", prompts[-1])
        text = output.getvalue()
        self.assertIn("Go-To-Market (GTM)", text)
        self.assertIn("Excluded: Kim", text)
        self.assertIn("Total", text)
        self.assertIn("| Total    |      1 |", text)

    def test_one_level_down_decline_adds_nobody(self):
        answers = iter([TERI, "teams", "__done__"])
        emails = admin.gather_member_emails(
            WORKERS,
            [WORKERS[0], WORKERS[1]],
            pick=lambda _entries, _header: next(answers),
            pick_level=lambda _entries, **_kwargs: ("done", None),
            team_name="Go-To-Market (GTM)",
            input_fn=lambda _prompt: "n",
        )
        self.assertEqual(emails, [])

    def test_one_level_down_cancel_does_not_ask_to_add(self):
        answers = iter([TERI, "teams", "__done__"])

        def refuse(_prompt):
            raise AssertionError("cancel should not ask to add people")

        emails = admin.gather_member_emails(
            WORKERS,
            [WORKERS[0], WORKERS[1]],
            pick=lambda _entries, _header: next(answers),
            pick_level=lambda _entries, **_kwargs: ("cancel", None),
            team_name="Go-To-Market (GTM)",
            input_fn=refuse,
        )
        self.assertEqual(emails, [])

    def test_leader_choices_say_what_each_option_adds(self):
        header, choices = admin.leader_add_choices(_person(TERI, "Teri Hatfield", SAM), WORKERS)
        self.assertEqual(header, "Teri Hatfield: who should be added?")
        self.assertEqual(choices[0], ("teams", "Choose from 2 people one level down"))
        self.assertEqual(choices[1], ("whole", "Add all 4 people, including Teri Hatfield"))
        lone = [
            ReportingWorker(ALEX, "Alex", 1, SAM, ""),
            ReportingWorker(PAT, "Pat", 1, ALEX, ""),
        ]
        _header, lone_choices = admin.leader_add_choices(lone[0], lone)
        self.assertEqual(lone_choices[0][1], "Choose from 1 person one level down")
        self.assertEqual(lone_choices[1][1], "Add all 2 people, including Alex")

    def test_people_list_lines_the_title_up_with_the_name(self):
        people = [
            ReportingWorker(ALEX, "Alex", 1, SAM, "Vice President"),
            ReportingWorker(TERI, "Teri Hatfield", 1, SAM, "Chief Revenue Officer"),
        ]
        alex, teri = (label for _email, label in admin._choices(people))
        column_line = admin.people_list_header("Add someone", people).splitlines()[-1]
        title_at = teri.index("Chief Revenue Officer")
        self.assertEqual(alex.index("Vice President"), title_at)
        self.assertEqual(column_line.index("title"), title_at)
        self.assertTrue(alex.startswith("Alex"))
        self.assertNotIn("·", alex)

    def test_a_direct_report_with_no_team_is_added_alone(self):
        answers = iter([ALEX, "__done__"])

        emails = admin.gather_member_emails(
            WORKERS,
            [WORKERS[0], WORKERS[1]],
            pick=lambda entries, header: next(answers),
            pick_multi=lambda entries, header: [],
        )
        self.assertEqual(emails, [ALEX])

    def test_add_team_members_sends_at_most_100_people_per_request(self):
        client = _Client()
        member_ids = [str(index) for index in range(201)]
        admin.add_team_members(client, "gtm", member_ids)
        batches = [call[2]["add"] for call in client.calls if call[0] == "PATCH"]
        self.assertEqual([len(batch) for batch in batches], [100, 100, 1])
        self.assertEqual([member for batch in batches for member in batch], member_ids)
        self.assertTrue(all(call[1] == "/v2/teams/gtm/members" for call in client.calls if call[0] == "PATCH"))

    def test_license_change_skips_every_other_account_type(self):
        members = [
            {"email": "a@iterable.com", "memberType": "View", "memberId": "1"},
            {"email": "b@iterable.com", "memberType": "Act+", "memberId": "2"},
            {"email": "c@iterable.com", "memberType": "view", "memberId": "3"},
            {"email": "d@iterable.com", "memberType": "Build", "memberId": "4"},
        ]
        changing = admin.members_on_license(members, "View")
        self.assertEqual([row["email"] for row in changing], ["a@iterable.com", "c@iterable.com"])

    def test_adding_people_uses_the_team_that_already_exists(self):
        seen = []
        answers = iter(["add", "gtm", ALEX, "__done__"])

        def pick(entries, header):
            seen.append((header, entries))
            return next(answers)

        client = _Client()
        with (
            patch.object(admin.SnowflakeWorkerStore, "fetch_reporting_tree", return_value=WORKERS),
            patch("sys.stdin.isatty", return_value=True),
        ):
            code = admin.run_create(client, input_fn=lambda _prompt: "", pick=pick, pick_multi=_no_multi)
        self.assertEqual(code, 0)
        self.assertEqual(seen[0][0], "Create a new team, or add people to one?")
        self.assertEqual(
            seen[0][1],
            [("new", "Create a new team"), ("add", "Add people to a team")],
        )
        self.assertNotIn("POST", [call[0] for call in client.calls])
        self.assertIn(("PATCH", "/v2/teams/gtm/members", {"add": ["m-alex"]}), client.calls)

    def test_creating_a_team_still_asks_for_a_name(self):
        seen = []
        answers = iter(["new", ALEX, "__done__"])

        def pick(entries, header):
            seen.append(header)
            return next(answers)

        client = _Client()
        with (
            patch.object(admin.SnowflakeWorkerStore, "fetch_reporting_tree", return_value=WORKERS),
            patch("sys.stdin.isatty", return_value=True),
        ):
            code = admin.run_create(
                client,
                input_fn=lambda _prompt: "Fresh Team",
                pick=pick,
                pick_multi=_no_multi,
            )
        self.assertEqual(code, 0)
        self.assertEqual(seen[0], "Create a new team, or add people to one?")
        self.assertIn(
            ("POST", "/v2/teams", {"name": "Fresh Team", "createTeamFolder": True, "visibility": "public"}),
            client.calls,
        )
        self.assertIn(("PATCH", "/v2/teams/new-id/members", {"add": ["m-alex"]}), client.calls)

    def test_udi_python_is_used_only_when_snowflake_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / "python"
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o755)
            self.assertEqual(
                admin.udi_reexec_python(snowflake_present=False, already_reexeced=False, udi_python=python),
                python,
            )
            self.assertIsNone(
                admin.udi_reexec_python(snowflake_present=True, already_reexeced=False, udi_python=python)
            )
            self.assertIsNone(
                admin.udi_reexec_python(snowflake_present=False, already_reexeced=True, udi_python=python)
            )
            missing = Path(tmp) / "missing"
            self.assertIsNone(
                admin.udi_reexec_python(snowflake_present=False, already_reexeced=False, udi_python=missing)
            )

    def test_team_roster_user_id_finds_the_member_license(self):
        by_id = {"u1": {"memberId": "u1", "memberType": "View", "email": "a@iterable.com"}}
        found = admin.members_from_roster([{"userId": "u1", "isTeamAdmin": False}], by_id)
        self.assertEqual(admin.license_counts(found), {"View": 1})

    def test_pivot_moves_only_the_from_license(self):
        types = {"View": "View", "Act+": "Act", "Build": "Build"}
        before = admin.with_all_license_types({"View": 2, "Act+": 1}, types)
        after = admin.counts_after_move(before, "View", "Act+")
        rows = {
            cells[0]: cells
            for line in admin.license_pivot(before, after).splitlines()
            if (cells := [part.strip() for part in line.strip("|").split("|")]) and cells[0] in types
        }
        self.assertEqual(rows["View"], ["View", "2", "0"])
        self.assertEqual(rows["Act+"], ["Act+", "1", "3"])
        self.assertEqual(rows["Build"], ["Build", "0", "0"])

    def test_license_change_confirms_the_before_and_after_pivot(self):
        client = _LicenseClient()
        picks = iter(["gtm", "View", "Act+"])
        answers = iter(["y", "n"])
        prompts = []
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("sys.stdin.isatty", return_value=True),
        ):
            code = admin.run_licenses(
                client,
                input_fn=lambda prompt: prompts.append(prompt) or next(answers),
                pick=lambda _entries, _header: next(picks),
            )
        self.assertEqual(code, 0)
        self.assertEqual(client.updates, [])
        self.assertIn("Apply View → Act+ for 2 people on Go-To-Market (GTM)?", prompts[-1])
        text = output.getvalue()
        self.assertIn("Before", text)
        self.assertIn("After", text)
        self.assertIn("Other teams stay.", text)

    def test_registry_lists_both_sigma_commands(self):
        from de_cli.menu_groups import menu_group_for_tool
        from de_cli.registry import find_tool

        create = find_tool("sigma-create-team")
        licenses = find_tool("sigma-team-licenses")
        assert create is not None and licenses is not None
        self.assertEqual(menu_group_for_tool(create), "sigma")
        self.assertEqual(menu_group_for_tool(licenses), "sigma")
        self.assertEqual(create.menu_summary, "Create team")
        self.assertEqual(licenses.menu_summary, "Team license management")
        self.assertEqual(create.default_args, ("create",))
        self.assertEqual(licenses.default_args, ("licenses",))
        self.assertTrue(licenses.menu_requires_confirm)


if __name__ == "__main__":
    unittest.main()
