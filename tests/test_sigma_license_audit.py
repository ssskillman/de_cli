import importlib.util
import io
import json
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "sigma_license_audit.py"
SPEC = importlib.util.spec_from_file_location("sigma_license_audit", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
import sys
sys.modules["sigma_license_audit"] = MODULE
assert SPEC.loader
SPEC.loader.exec_module(MODULE)

DepartedWorker = MODULE.DepartedWorker
UserActivity = MODULE.UserActivity
MemberAuditRecord = MODULE.MemberAuditRecord
derive_tier_from_permissions = MODULE.derive_tier_from_permissions
build_audit_records = MODULE.build_audit_records
AuditReporter = MODULE.AuditReporter
SnowflakeWorkerStore = MODULE.SnowflakeWorkerStore
ActivityStore = MODULE.ActivityStore
run_remediation_plan = MODULE.run_remediation_plan
BrailleSpinner = MODULE.BrailleSpinner
fzf_pick_choice = MODULE.fzf_pick_choice
fzf_pick_multi = MODULE.fzf_pick_multi
ReportingWorker = MODULE.ReportingWorker
build_team_license_rows = MODULE.build_team_license_rows
render_team_license_report = MODULE.render_team_license_report


class FakeSigmaClient:
    def __init__(self, members=None):
        self.members = list(members or [])
        self.updates = []
        self.deactivations = []

    def load_members(self):
        return list(self.members)

    def update_member_type(self, member_id: str, member_type: str = "View"):
        self.updates.append((member_id, member_type))
        return {"status": "ok"}

    def deactivate_member(self, member_id: str, new_owner_id=None, archive_docs=False):
        self.deactivations.append((member_id, new_owner_id, archive_docs))
        return {"status": "ok"}


class SigmaLicenseAuditTests(unittest.TestCase):
    def test_gtm_team_separates_direct_and_indirect_license_breakdown(self):
        workers = [
            ReportingWorker(
                email="direct.builder@iterable.com",
                full_name="Direct Builder",
                depth=1,
                manager_email="teri.hatfield@iterable.com",
            ),
            ReportingWorker(
                email="indirect.viewer@iterable.com",
                full_name="Indirect Viewer",
                depth=2,
                manager_email="direct.builder@iterable.com",
            ),
            ReportingWorker(
                email="indirect.no.sigma@iterable.com",
                full_name="Indirect No Sigma",
                depth=3,
                manager_email="indirect.viewer@iterable.com",
            ),
        ]
        sigma_records = [
            MemberAuditRecord(
                email="direct.builder@iterable.com",
                member_id="m-1",
                first_name="Direct",
                last_name="Builder",
                member_type="Admin",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            ),
            MemberAuditRecord(
                email="indirect.viewer@iterable.com",
                member_id="m-2",
                first_name="Indirect",
                last_name="Viewer",
                member_type="View",
                license_type="View",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            ),
        ]

        rows = build_team_license_rows(workers, sigma_records)
        self.assertEqual([row.relationship for row in rows], ["Direct", "Indirect", "Indirect"])
        self.assertEqual(rows[-1].license_type, "No Sigma account")

        report = render_team_license_report(
            "GTM Team (Teri Hatfield)", rows, manager_email="teri.hatfield@iterable.com"
        )
        self.assertIn("GTM Team (Teri Hatfield)", report)
        table_rows = {
            line.split("|")[1].strip(): [cell.strip() for cell in line.split("|")[2:-1]]
            for line in report.splitlines()
            if line.startswith("| ") and line.split("|")[1].strip() in {"Direct", "Indirect", "Total"}
        }
        self.assertEqual(table_rows["Direct"], ["1", "1", "0", "0", "0", "0"])
        self.assertEqual(table_rows["Indirect"], ["2", "0", "0", "0", "1", "1"])
        self.assertEqual(table_rows["Total"], ["3", "1", "0", "0", "1", "1"])
        self.assertIn("| Relationship |", report)
        self.assertIn("indirect.no.sigma@iterable.com", report)

    def test_employee_list_includes_job_title(self):
        rows = build_team_license_rows(
            [
                ReportingWorker(
                    "ada@iterable.com",
                    "Lovelace, Ada",
                    1,
                    "teri.hatfield@iterable.com",
                    "Staff Engineer",
                )
            ],
            [],
        )
        listing = MODULE.render_team_employee_list(rows)
        self.assertIn("Job title", listing)
        self.assertIn("Staff Engineer", listing)

    def test_detailed_user_list_is_shown_only_after_yes(self):
        rows = build_team_license_rows(
            [ReportingWorker("ada@iterable.com", "Lovelace, Ada", 1, "teri.hatfield@iterable.com")],
            [],
        )
        prompts = []
        declined = MODULE.offer_show_employee_list(
            rows,
            input_fn=lambda prompt: prompts.append(prompt) or "n",
            isatty=True,
        )
        self.assertIsNone(declined)
        self.assertIn("detailed user list", prompts[0].lower())

        shown = MODULE.offer_show_employee_list(
            rows,
            input_fn=lambda _prompt: "y",
            isatty=True,
        )
        self.assertIn("ada@iterable.com", shown)
        self.assertIn("Employees", shown)

        def explode(_prompt):
            raise AssertionError("prompted without a tty")

        skipped = MODULE.offer_show_employee_list(
            rows,
            input_fn=explode,
            isatty=False,
        )
        self.assertIsNone(skipped)

    def test_value_engineering_reports_to_katie_barends(self):
        self.assertEqual(MODULE.VE_MANAGER_EMAIL, "katie.barends@iterable.com")
        self.assertEqual(MODULE.VE_TEAM_LABEL, "Value Engineering (Katie Barends)")

    def test_yes_adds_missing_people_as_view_and_reprints_breakdown(self):
        workers = [
            ReportingWorker("ada@iterable.com", "Lovelace, Ada", 1, "teri.hatfield@iterable.com"),
            ReportingWorker("no.sigma@iterable.com", "Barends, Katie Leigh", 2, "ada@iterable.com"),
        ]
        records = [
            MemberAuditRecord(
                email="ada@iterable.com",
                member_id="m-1",
                first_name="Ada",
                last_name="Lovelace",
                member_type="Admin",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            )
        ]
        prompts = []

        class CreatingClient:
            def __init__(self):
                self.members = [
                    {
                        "memberId": "m-1",
                        "email": "ada@iterable.com",
                        "firstName": "Ada",
                        "lastName": "Lovelace",
                        "memberType": "Admin",
                        "userKind": "internal",
                        "isArchived": False,
                        "isInactive": False,
                    }
                ]
                self.created = []

            def load_account_types_map(self):
                return {"Admin": "Build", "View": "View"}

            def load_members(self):
                return list(self.members)

            def create_member(self, email, first_name, last_name, member_type="View"):
                created = {
                    "memberId": "m-new",
                    "email": email,
                    "firstName": first_name,
                    "lastName": last_name,
                    "memberType": member_type,
                    "userKind": "internal",
                    "isArchived": False,
                    "isInactive": False,
                }
                self.created.append(created)
                self.members.append(created)
                return created

        client = CreatingClient()

        def answer(prompt):
            prompts.append(prompt)
            return "y"

        updated = MODULE.offer_missing_view_accounts(
            workers=workers,
            records=records,
            client=client,
            type_to_tier={"Admin": "Build", "View": "View"},
            label="GTM Team (Teri Hatfield)",
            manager_email="teri.hatfield@iterable.com",
            input_fn=answer,
            isatty=True,
        )
        self.assertIn("no Sigma license", prompts[0])
        self.assertIn("View", prompts[1])
        self.assertEqual(client.created[0]["email"], "no.sigma@iterable.com")
        self.assertEqual(client.created[0]["firstName"], "Katie Leigh")
        self.assertEqual(client.created[0]["lastName"], "Barends")
        self.assertEqual(client.created[0]["memberType"], "View")
        self.assertIn("Updated license breakdown", updated)
        table_rows = {
            line.split("|")[1].strip(): [cell.strip() for cell in line.split("|")[2:-1]]
            for line in updated.splitlines()
            if line.startswith("| ") and line.split("|")[1].strip() in {"Direct", "Indirect", "Total"}
        }
        self.assertEqual(table_rows["Total"], ["2", "1", "0", "0", "1", "0"])

    def test_unlicensed_list_can_be_shown_without_creating_view_accounts(self):
        workers = [
            ReportingWorker("ada@iterable.com", "Lovelace, Ada", 1, "teri.hatfield@iterable.com", "VP"),
            ReportingWorker("no.sigma@iterable.com", "Missing Person", 2, "ada@iterable.com", "Analyst"),
        ]
        records = [
            MemberAuditRecord(
                email="ada@iterable.com",
                member_id="m-1",
                first_name="Ada",
                last_name="Lovelace",
                member_type="Admin",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            )
        ]
        missing = [
            row
            for row in build_team_license_rows(workers, records)
            if row.license_type == "No Sigma account"
        ]
        listing = MODULE.render_team_employee_list(missing, title="People with no Sigma license")
        self.assertIn("People with no Sigma license", listing)
        self.assertIn("Analyst", listing)
        self.assertIn("no.sigma@iterable.com", listing)
        self.assertNotIn("ada@iterable.com", listing)

        class CreatingClient:
            def __init__(self):
                self.created = []

            def create_member(self, *args, **kwargs):
                self.created.append((args, kwargs))

        prompts = []
        answers = iter(["y", "n"])
        declined = MODULE.offer_missing_view_accounts(
            workers=workers,
            records=records,
            client=CreatingClient(),
            type_to_tier={"View": "View"},
            label="GTM Team (Teri Hatfield)",
            manager_email="teri.hatfield@iterable.com",
            input_fn=lambda prompt: prompts.append(prompt) or next(answers),
            isatty=True,
        )
        self.assertIsNone(declined)
        self.assertIn("no Sigma license", prompts[0])
        self.assertIn("View", prompts[1])

    def test_no_and_non_tty_do_not_create_view_accounts(self):
        workers = [
            ReportingWorker("no.sigma@iterable.com", "Missing Person", 1, "katie.barends@iterable.com"),
        ]

        class CreatingClient:
            def __init__(self):
                self.created = []

            def create_member(self, *args, **kwargs):
                self.created.append((args, kwargs))

        client = CreatingClient()
        declined = MODULE.offer_missing_view_accounts(
            workers=workers,
            records=[],
            client=client,
            type_to_tier={"View": "View"},
            label="Value Engineering (Katie Barends)",
            manager_email="katie.barends@iterable.com",
            input_fn=lambda _prompt: "n",
            isatty=True,
        )
        self.assertIsNone(declined)
        self.assertEqual(client.created, [])

        def explode(_prompt):
            raise AssertionError("prompted without a tty")

        skipped = MODULE.offer_missing_view_accounts(
            workers=workers,
            records=[],
            client=client,
            type_to_tier={"View": "View"},
            label="Value Engineering (Katie Barends)",
            manager_email="katie.barends@iterable.com",
            input_fn=explode,
            isatty=False,
        )
        self.assertIsNone(skipped)
        self.assertEqual(client.created, [])

    def test_yes_upgrades_view_team_members_to_act_plus(self):
        workers = [
            ReportingWorker("view.user@iterable.com", "View User", 1, "teri.hatfield@iterable.com"),
            ReportingWorker("builder@iterable.com", "Builder User", 1, "teri.hatfield@iterable.com"),
        ]
        records = [
            MemberAuditRecord(
                email="view.user@iterable.com",
                member_id="m-view",
                first_name="View",
                last_name="User",
                member_type="View",
                license_type="View",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            ),
            MemberAuditRecord(
                email="builder@iterable.com",
                member_id="m-build",
                first_name="Builder",
                last_name="User",
                member_type="build",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            ),
        ]
        prompts = []

        class UpgradingClient:
            def __init__(self):
                self.members = [
                    {
                        "memberId": "m-view",
                        "email": "view.user@iterable.com",
                        "firstName": "View",
                        "lastName": "User",
                        "memberType": "View",
                        "userKind": "internal",
                        "isArchived": False,
                        "isInactive": False,
                    },
                    {
                        "memberId": "m-build",
                        "email": "builder@iterable.com",
                        "firstName": "Builder",
                        "lastName": "User",
                        "memberType": "build",
                        "userKind": "internal",
                        "isArchived": False,
                        "isInactive": False,
                    },
                ]
                self.updates = []

            def load_members(self):
                return list(self.members)

            def update_member_type(self, member_id, member_type="View"):
                self.updates.append((member_id, member_type))
                for member in self.members:
                    if member["memberId"] == member_id:
                        member["memberType"] = member_type
                return {"status": "ok"}

        client = UpgradingClient()
        updated = MODULE.offer_upgrade_view_to_act_plus(
            workers=workers,
            records=records,
            client=client,
            type_to_tier={"View": "View", "Act+": "Act", "Act": "Build", "build": "Build"},
            label="GTM Team (Teri Hatfield)",
            manager_email="teri.hatfield@iterable.com",
            input_fn=lambda prompt: prompts.append(prompt) or "y",
            isatty=True,
        )
        self.assertIn("Act+", prompts[0])
        self.assertEqual(client.updates, [("m-view", "Act+")])
        self.assertIn("Updated license breakdown", updated)
        table_rows = {
            line.split("|")[1].strip(): [cell.strip() for cell in line.split("|")[2:-1]]
            for line in updated.splitlines()
            if line.startswith("| ") and line.split("|")[1].strip() in {"Direct", "Indirect", "Total"}
        }
        self.assertEqual(table_rows["Total"], ["2", "1", "0", "1", "0", "0"])

    def test_no_does_not_upgrade_view_members_to_act_plus(self):
        workers = [
            ReportingWorker("view.user@iterable.com", "View User", 1, "katie.barends@iterable.com"),
        ]
        records = [
            MemberAuditRecord(
                email="view.user@iterable.com",
                member_id="m-view",
                first_name="View",
                last_name="User",
                member_type="View",
                license_type="View",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            )
        ]

        class UpgradingClient:
            def __init__(self):
                self.updates = []

            def update_member_type(self, member_id, member_type="View"):
                self.updates.append((member_id, member_type))

        client = UpgradingClient()
        declined = MODULE.offer_upgrade_view_to_act_plus(
            workers=workers,
            records=records,
            client=client,
            type_to_tier={"Act+": "Act"},
            label="Value Engineering (Katie Barends)",
            manager_email="katie.barends@iterable.com",
            input_fn=lambda _prompt: "n",
            isatty=True,
        )
        self.assertIsNone(declined)
        self.assertEqual(client.updates, [])

    def _subteam_fixture(self):
        workers = [
            ReportingWorker("katie.barends@iterable.com", "Barends, Katie Leigh", 1, "teri.hatfield@iterable.com"),
            ReportingWorker("ada@iterable.com", "Lovelace, Ada", 2, "katie.barends@iterable.com"),
            ReportingWorker("no.sigma@iterable.com", "Missing Person", 3, "ada@iterable.com"),
            ReportingWorker("seth.charles@iterable.com", "Charles, Seth", 1, "teri.hatfield@iterable.com"),
        ]
        records = [
            MemberAuditRecord(
                email="ada@iterable.com",
                member_id="m-ada",
                first_name="Ada",
                last_name="Lovelace",
                member_type="View",
                license_type="View",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            )
        ]
        return workers, records

    def test_subteam_breakdown_includes_everyone_below_the_lead(self):
        workers, records = self._subteam_fixture()
        leads = MODULE.direct_subteam_leads(workers, "teri.hatfield@iterable.com")
        self.assertEqual([lead.email for lead in leads], ["katie.barends@iterable.com"])
        subtree = MODULE.rebase_subtree(workers, "katie.barends@iterable.com")
        self.assertEqual([(worker.email, worker.depth) for worker in subtree], [
            ("ada@iterable.com", 1),
            ("no.sigma@iterable.com", 2),
        ])
        summary = MODULE.render_subteam_summary(
            workers, records, "teri.hatfield@iterable.com"
        )
        self.assertIn("Barends, Katie Leigh", summary)
        self.assertIn("Sub teams", summary)
        table_rows = {
            line.split("|")[1].strip(): [cell.strip() for cell in line.split("|")[2:-1]]
            for line in summary.splitlines()
            if line.startswith("| ") and "Barends" in line
        }
        self.assertEqual(table_rows["Barends, Katie Leigh"], ["2", "0", "0", "0", "1", "1"])

    def test_gtm_tree_starts_at_directs_and_refreshes_after_each_exclusion(self):
        workers = [
            ReportingWorker(
                "katie.barends@iterable.com",
                "Barends, Katie Leigh",
                1,
                "teri.hatfield@iterable.com",
                job_title="VP, Value Engineering",
            ),
            ReportingWorker(
                "ada@iterable.com",
                "Lovelace, Ada",
                2,
                "katie.barends@iterable.com",
                job_title="Engineer",
            ),
            ReportingWorker("dee@iterable.com", "Third, Dee", 3, "ada@iterable.com"),
            ReportingWorker(
                "seth.charles@iterable.com",
                "Charles, Seth",
                1,
                "teri.hatfield@iterable.com",
                job_title="Director, Sales",
            ),
            ReportingWorker("sam@iterable.com", "Sales, Sam", 2, "seth.charles@iterable.com"),
            ReportingWorker("outsider@iterable.com", "Outside, Olivia", 1, "someone.else@iterable.com"),
        ]
        seen = []
        actions = iter([
            ("exclude", "katie.barends@iterable.com"),
            ("drill", "seth.charles@iterable.com"),
            ("exclude", "sam@iterable.com"),
            ("done", None),
        ])

        def pick_level(entries, **kwargs):
            seen.append((kwargs["header"], [email for email, _label in entries]))
            return next(actions)

        prompts = []

        def input_fn(prompt):
            prompts.append(prompt)
            return "y" if "detailed user list" in prompt else "n"

        output = io.StringIO()
        with redirect_stdout(output):
            presented = MODULE.present_selected_reporting_teams(
                workers=workers,
                records=[],
                manager_email="teri.hatfield@iterable.com",
                manager_label="GTM Team (Teri Hatfield)",
                input_fn=input_fn,
                pick_level=pick_level,
                isatty=True,
            )

        text = output.getvalue()
        self.assertTrue(presented)
        self.assertEqual(
            seen[0][1],
            ["katie.barends@iterable.com", "seth.charles@iterable.com"],
        )
        self.assertIn("their directs", seen[0][0])
        self.assertEqual(seen[2][1], ["sam@iterable.com"])
        self.assertGreaterEqual(text.count("License breakdown"), 2)
        employees = text.split("Employees", 1)[-1]
        self.assertIn("Charles, Seth", employees)
        self.assertNotIn("Sales, Sam", employees)
        self.assertNotIn("Barends, Katie Leigh", employees)
        self.assertNotIn("Lovelace, Ada", employees)
        self.assertNotIn("Third, Dee", employees)
        self.assertNotIn("Outside, Olivia", text)
        self.assertTrue(any("detailed user list" in prompt for prompt in prompts))

    def test_right_arrow_level_can_include_one_reporting_tree(self):
        workers = [
            ReportingWorker(
                "katie.barends@iterable.com",
                "Barends, Katie Leigh",
                1,
                "teri.hatfield@iterable.com",
                job_title="VP, Value Engineering",
            ),
            ReportingWorker(
                "ada@iterable.com",
                "Lovelace, Ada",
                2,
                "katie.barends@iterable.com",
                job_title="Engineer",
            ),
            ReportingWorker("dee@iterable.com", "Third, Dee", 3, "ada@iterable.com"),
        ]
        actions = iter([
            ("exclude", "katie.barends@iterable.com"),
            ("drill", "katie.barends@iterable.com"),
            ("include", "ada@iterable.com"),
            ("done", None),
        ])
        output = io.StringIO()
        with redirect_stdout(output):
            MODULE.present_selected_reporting_teams(
                workers=workers,
                records=[],
                manager_email="teri.hatfield@iterable.com",
                manager_label="GTM Team (Teri Hatfield)",
                input_fn=lambda prompt: "y" if "detailed user list" in prompt else "n",
                pick_level=lambda _entries, **_kwargs: next(actions),
                isatty=True,
            )
        employees = output.getvalue().split("Employees", 1)[-1]
        self.assertIn("Lovelace, Ada", employees)
        self.assertIn("Third, Dee", employees)
        self.assertNotIn("Barends, Katie Leigh", employees)

    def test_leaving_the_gtm_tree_unchanged_keeps_the_full_list_prompt(self):
        workers, _records = self._subteam_fixture()

        presented = MODULE.present_selected_reporting_teams(
            workers=workers,
            records=[],
            manager_email="teri.hatfield@iterable.com",
            manager_label="GTM Team (Teri Hatfield)",
            pick_level=lambda _entries, **_kwargs: ("done", None),
            isatty=True,
        )
        self.assertFalse(presented)

    def test_subteam_fzf_upgrades_only_the_selected_view_member(self):
        workers, records = self._subteam_fixture()

        class UpgradingClient:
            def __init__(self):
                self.members = [
                    {
                        "memberId": "m-ada",
                        "email": "ada@iterable.com",
                        "firstName": "Ada",
                        "lastName": "Lovelace",
                        "memberType": "View",
                        "userKind": "internal",
                        "isArchived": False,
                        "isInactive": False,
                    }
                ]
                self.updates = []

            def load_members(self):
                return list(self.members)

            def update_member_type(self, member_id, member_type="View"):
                self.updates.append((member_id, member_type))
                for member in self.members:
                    if member["memberId"] == member_id:
                        member["memberType"] = member_type
                return {"memberType": member_type}

        client = UpgradingClient()
        choices = iter(["katie.barends@iterable.com", "people", "q"])

        def pick_choice(entries, **_kwargs):
            return next(choices)

        def pick_multi(entries, **_kwargs):
            self.assertEqual([key for key, _label in entries], ["ada@iterable.com"])
            return ["ada@iterable.com"]

        MODULE.browse_subteams(
            workers=workers,
            records=records,
            client=client,
            type_to_tier={"View": "View", "Act+": "Act", "Act": "Build"},
            manager_email="teri.hatfield@iterable.com",
            manager_label="GTM Team (Teri Hatfield)",
            input_fn=lambda _prompt: "y",
            pick_choice=pick_choice,
            pick_multi=pick_multi,
            isatty=True,
        )
        self.assertEqual(client.updates, [("m-ada", "Act+")])

    def test_cascade_stops_at_the_lowest_team_and_upgrades_its_viewers(self):
        workers, records = self._subteam_fixture()
        workers = [
            *workers,
            ReportingWorker("leaf@iterable.com", "Leaf Viewer", 3, "ada@iterable.com"),
        ]
        records = [
            *records,
            MemberAuditRecord(
                email="leaf@iterable.com",
                member_id="m-leaf",
                first_name="Leaf",
                last_name="Viewer",
                member_type="View",
                license_type="View",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            ),
        ]

        class UpgradingClient:
            def __init__(self):
                self.members = [
                    {
                        "memberId": "m-ada",
                        "email": "ada@iterable.com",
                        "firstName": "Ada",
                        "lastName": "Lovelace",
                        "memberType": "View",
                        "userKind": "internal",
                        "isArchived": False,
                        "isInactive": False,
                    },
                    {
                        "memberId": "m-leaf",
                        "email": "leaf@iterable.com",
                        "firstName": "Leaf",
                        "lastName": "Viewer",
                        "memberType": "View",
                        "userKind": "internal",
                        "isArchived": False,
                        "isInactive": False,
                    },
                ]
                self.updates = []

            def load_members(self):
                return list(self.members)

            def update_member_type(self, member_id, member_type="View"):
                self.updates.append((member_id, member_type))
                for member in self.members:
                    if member["memberId"] == member_id:
                        member["memberType"] = member_type
                return {"memberType": member_type}

        client = UpgradingClient()
        choices = iter([
            "katie.barends@iterable.com",
            "cascade",
            "ada@iterable.com",
            "upgrade",
            "q",
            "q",
        ])

        def pick_choice(entries, **_kwargs):
            keys = [key for key, _label in entries]
            choice = next(choices)
            if choice == "upgrade":
                self.assertNotIn("cascade", keys)
                self.assertIn("upgrade", keys)
            return choice

        MODULE.browse_subteams(
            workers=workers,
            records=records,
            client=client,
            type_to_tier={"View": "View", "Act+": "Act", "Act": "Build"},
            manager_email="teri.hatfield@iterable.com",
            manager_label="GTM Team (Teri Hatfield)",
            input_fn=lambda _prompt: "y",
            pick_choice=pick_choice,
            pick_multi=lambda entries, **_kwargs: [],
            isatty=True,
        )
        self.assertEqual(client.updates, [("m-leaf", "Act+")])

    def test_audit_menu_offers_departed_seats_then_alphabetical_actions(self):
        entries = MODULE.audit_menu_entries(departed_active_paid=2, departed_active_free=98)
        self.assertEqual(entries[0][0], "critical-paid")
        self.assertIn("[CRITICAL] Active + Paid: 2", entries[0][1])
        self.assertEqual(entries[1][0], "high-free")
        self.assertIn("[HIGH] Active + Free: 98", entries[1][1])
        self.assertEqual(entries[-1], ("q", "q. Quit"))
        labels = [label.split(". ", 1)[1] for _key, label in entries[2:-1]]
        self.assertEqual(labels, sorted(labels, key=str.casefold))
        self.assertTrue(labels[0].startswith("Batch deactivate"))
        self.assertIn("Current Iterable employees with no Sigma license", labels)
        self.assertTrue(labels[-1].startswith("Value Engineering"))

    def test_unlicensed_employees_can_all_get_view_or_a_chosen_subset(self):
        ActiveEmployee = MODULE.ActiveEmployee
        employees = [
            ActiveEmployee("ada@iterable.com", "Lovelace, Ada", "Engineer"),
            ActiveEmployee("no.sigma@iterable.com", "Barends, Katie Leigh", "VP, Value Engineering"),
            ActiveEmployee("other.missing@iterable.com", "Skillman, Shawn", "Analyst"),
        ]
        records = [
            MemberAuditRecord(
                email="ada@iterable.com",
                member_id="m-ada",
                first_name="Ada",
                last_name="Lovelace",
                member_type="View",
                license_type="View",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            )
        ]

        class CreatingClient:
            def __init__(self):
                self.created = []

            def load_account_types_map(self):
                return {"View": "View", "Act+": "Act"}

            def load_members(self):
                return []

            def create_member(self, email, first_name, last_name, member_type="View"):
                self.created.append(
                    {
                        "email": email,
                        "firstName": first_name,
                        "lastName": last_name,
                        "memberType": member_type,
                    }
                )
                return {}

        missing = MODULE.employees_without_sigma_account(employees, records)
        self.assertEqual(
            [person.email for person in missing],
            ["no.sigma@iterable.com", "other.missing@iterable.com"],
        )

        listed = io.StringIO()
        client = CreatingClient()
        with redirect_stdout(listed):
            created = MODULE.offer_view_to_unlicensed_employees(
                employees=employees,
                records=records,
                client=client,
                type_to_tier={"View": "View"},
                input_fn=lambda _prompt: "n",
                pick_choice=lambda _entries, **_kwargs: "all",
                pick_multi=lambda _entries, **_kwargs: [],
                isatty=True,
            )
        self.assertEqual(created, [])
        self.assertEqual(client.created, [])
        self.assertIn("Barends, Katie Leigh", listed.getvalue())
        self.assertIn("VP, Value Engineering", listed.getvalue())
        self.assertNotIn("Lovelace, Ada", listed.getvalue())

        client = CreatingClient()
        created = MODULE.offer_view_to_unlicensed_employees(
            employees=employees,
            records=records,
            client=client,
            type_to_tier={"View": "View"},
            input_fn=lambda _prompt: "y",
            pick_choice=lambda _entries, **_kwargs: "all",
            pick_multi=lambda _entries, **_kwargs: [],
            isatty=True,
        )
        self.assertEqual(
            created,
            ["no.sigma@iterable.com", "other.missing@iterable.com"],
        )
        self.assertEqual(client.created[0]["firstName"], "Katie Leigh")
        self.assertEqual(client.created[0]["lastName"], "Barends")
        self.assertEqual({row["memberType"] for row in client.created}, {"View"})

        client = CreatingClient()

        def pick_multi(entries, **_kwargs):
            self.assertEqual(
                [key for key, _label in entries],
                ["no.sigma@iterable.com", "other.missing@iterable.com"],
            )
            return ["other.missing@iterable.com"]

        created = MODULE.offer_view_to_unlicensed_employees(
            employees=employees,
            records=records,
            client=client,
            type_to_tier={"View": "View"},
            input_fn=lambda _prompt: "y",
            pick_choice=lambda _entries, **_kwargs: "pick",
            pick_multi=pick_multi,
            isatty=True,
        )
        self.assertEqual(created, ["other.missing@iterable.com"])
        self.assertEqual(client.created[0]["firstName"], "Shawn")
        self.assertEqual(client.created[0]["lastName"], "Skillman")

    def test_active_employee_email_history_does_not_use_fivetran_deleted(self):
        sql = MODULE.SnowflakeWorkerStore.SQL_ACTIVE_EMPLOYEES
        history, _, _worker = sql.partition("active_workers AS")
        self.assertNotIn("_FIVETRAN_DELETED", history)
        self.assertIn("w._FIVETRAN_DELETED", sql)

    def test_former_email_is_excluded_and_same_name_needs_review(self):
        ActiveEmployee = MODULE.ActiveEmployee
        employees = [
            ActiveEmployee(
                "caitlyn.tuel@iterable.com",
                "Tuel, Caitlyn",
                "Director, Customer Education",
                ("caitlyn.vestal@iterable.com",),
            ),
            ActiveEmployee("new.person@iterable.com", "Person, New", "Engineer"),
            ActiveEmployee("same.name@iterable.com", "Lovelace, Ada", "Engineer"),
        ]
        records = [
            MemberAuditRecord(
                email="caitlyn.vestal@iterable.com",
                member_id="m-caitlyn",
                first_name="Caitlyn",
                last_name="Vestal",
                member_type="View",
                license_type="View",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            ),
            MemberAuditRecord(
                email="ada@iterable.com",
                member_id="m-ada",
                first_name="Ada",
                last_name="Lovelace",
                member_type="View",
                license_type="View",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            ),
        ]
        review = MODULE.classify_employees_without_sigma(employees, records)
        self.assertEqual([person.email for person in review.confirmed_missing], ["new.person@iterable.com"])
        self.assertEqual(review.former_email_matches[0].employee.email, "caitlyn.tuel@iterable.com")
        self.assertEqual(review.former_email_matches[0].sigma_email, "caitlyn.vestal@iterable.com")
        self.assertEqual(review.possible_name_matches[0].employee.email, "same.name@iterable.com")
        self.assertEqual(review.possible_name_matches[0].sigma_email, "ada@iterable.com")

        class CreatingClient:
            def __init__(self, members):
                self.members = members
                self.created = []

            def load_members(self):
                return list(self.members)

            def load_account_types_map(self):
                return {"View": "View"}

            def create_member(self, email, first_name, last_name, member_type="View"):
                self.created.append(email)
                return {}

        client = CreatingClient([{"email": "caitlyn.vestal@iterable.com"}, {"email": "ada@iterable.com"}])
        listed = io.StringIO()
        with redirect_stdout(listed):
            created = MODULE.offer_view_to_unlicensed_employees(
                employees=employees,
                records=records,
                client=client,
                type_to_tier={"View": "View"},
                input_fn=lambda _prompt: "y",
                pick_choice=lambda _entries, **_kwargs: "all",
                pick_multi=lambda _entries, **_kwargs: [],
                isatty=True,
            )
        self.assertEqual(created, ["new.person@iterable.com"])
        self.assertEqual(client.created, ["new.person@iterable.com"])
        text = listed.getvalue()
        self.assertIn("caitlyn.tuel@iterable.com", text)
        self.assertIn("caitlyn.vestal@iterable.com", text)
        self.assertIn("same.name@iterable.com", text)

        client = CreatingClient([{"email": "caitlyn.vestal@iterable.com"}, {"email": "ada@iterable.com"}])

        def pick_multi(entries, **_kwargs):
            self.assertEqual([key for key, _label in entries], ["new.person@iterable.com", "same.name@iterable.com"])
            self.assertIn("possible name match", entries[1][1])
            return ["same.name@iterable.com"]

        created = MODULE.offer_view_to_unlicensed_employees(
            employees=employees,
            records=records,
            client=client,
            type_to_tier={"View": "View"},
            input_fn=lambda _prompt: "y",
            pick_choice=lambda _entries, **_kwargs: "pick",
            pick_multi=pick_multi,
            isatty=True,
        )
        self.assertEqual(created, ["same.name@iterable.com"])

    def test_grant_view_to_all_does_not_change_existing_accounts(self):
        ActiveEmployee = MODULE.ActiveEmployee
        employees = [
            ActiveEmployee("paid.build@iterable.com", "Builder, Pat", "Engineer"),
            ActiveEmployee("new.person@iterable.com", "Person, New", "Engineer"),
            ActiveEmployee("caitlyn.tuel@iterable.com", "Tuel, Caitlyn", "Director, Customer Education"),
        ]
        records = [
            MemberAuditRecord(
                email="caitlyn.vestal@iterable.com",
                member_id="m-caitlyn",
                first_name="Caitlyn",
                last_name="Tuel",
                member_type="Act+",
                license_type="Act",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
            )
        ]

        class GuardedClient:
            def __init__(self, members, fail_reload=False):
                self.members = members
                self.fail_reload = fail_reload
                self.created = []
                self.updates = []
                self.deactivated = []

            def load_members(self):
                if self.fail_reload:
                    raise RuntimeError("sigma unavailable")
                return list(self.members)

            def load_account_types_map(self):
                return {"View": "View", "build": "Build", "Act+": "Act"}

            def create_member(self, email, first_name, last_name, member_type="View"):
                self.created.append(
                    {"email": email, "memberType": member_type, "firstName": first_name, "lastName": last_name}
                )
                return {}

            def update_member_type(self, member_id, member_type="View"):
                self.updates.append((member_id, member_type))
                return {}

            def deactivate_member(self, member_id, new_owner_id=None, archive_docs=False):
                self.deactivated.append(member_id)
                return {}

        client = GuardedClient(
            [
                {"email": "paid.build@iterable.com", "memberId": "m-paid", "memberType": "build"},
                {"email": "caitlyn.vestal@iterable.com", "memberId": "m-caitlyn", "memberType": "Act+"},
            ]
        )
        listed = io.StringIO()
        with redirect_stdout(listed):
            created = MODULE.offer_view_to_unlicensed_employees(
                employees=employees,
                records=records,
                client=client,
                type_to_tier={"View": "View", "build": "Build", "Act+": "Act"},
                input_fn=lambda _prompt: "y",
                pick_choice=lambda _entries, **_kwargs: "all",
                pick_multi=lambda _entries, **_kwargs: [],
                isatty=True,
            )
        self.assertEqual(created, ["new.person@iterable.com"])
        self.assertEqual(client.created[0]["memberType"], "View")
        self.assertEqual(client.updates, [])
        self.assertEqual(client.deactivated, [])
        self.assertIn("paid.build@iterable.com", listed.getvalue())
        self.assertNotIn("caitlyn.tuel@iterable.com", [row["email"] for row in client.created])

        failed = GuardedClient([], fail_reload=True)
        created = MODULE.offer_view_to_unlicensed_employees(
            employees=employees,
            records=[],
            client=failed,
            type_to_tier={"View": "View"},
            input_fn=lambda _prompt: "y",
            pick_choice=lambda _entries, **_kwargs: "all",
            pick_multi=lambda _entries, **_kwargs: [],
            isatty=True,
        )
        self.assertEqual(created, [])
        self.assertEqual(failed.created, [])
        self.assertEqual(failed.updates, [])

    def test_subteam_screens_show_the_path_from_the_root_team(self):
        workers, _records = self._subteam_fixture()
        headers = []
        choices = iter([
            "katie.barends@iterable.com",
            "cascade",
            "ada@iterable.com",
            "back",
            "q",
            "q",
        ])

        def pick_choice(_entries, **kwargs):
            headers.append(kwargs["header"])
            return next(choices)

        output = io.StringIO()
        with redirect_stdout(output):
            MODULE.browse_subteams(
                workers=workers,
                records=[],
                client=object(),
                type_to_tier=None,
                manager_email="teri.hatfield@iterable.com",
                manager_label="GTM Team (Teri Hatfield)",
                input_fn=lambda _prompt: "y",
                pick_choice=pick_choice,
                pick_multi=lambda _entries, **_kwargs: [],
                isatty=True,
            )

        text = output.getvalue()
        root = "GTM Team (Teri Hatfield)"
        katie = f"{root} > Barends, Katie Leigh"
        ada = f"{katie} > Lovelace, Ada"
        self.assertIn(katie, text)
        self.assertIn(ada, text)
        self.assertEqual(
            headers,
            [
                f"{root} · counts include everyone below the lead",
                f"{katie} · upgrade all viewers or cascade down",
                f"{katie} · counts include everyone below the lead",
                f"{ada} · upgrade all viewers or cascade down",
                f"{katie} · counts include everyone below the lead",
                f"{root} · counts include everyone below the lead",
            ],
        )

    def test_derive_tier_from_permissions(self):
        self.assertEqual(derive_tier_from_permissions(["edit-workbook"]), "Build")
        self.assertEqual(derive_tier_from_permissions(["manage-users", "view-connections"]), "Build")
        self.assertEqual(derive_tier_from_permissions(["full-explore"]), "Analyze")
        self.assertEqual(derive_tier_from_permissions(["export-to-slack"]), "Act")
        self.assertEqual(derive_tier_from_permissions(["unknown-perm"]), "View")
        self.assertEqual(derive_tier_from_permissions([]), "View")
        # Dict permissions support
        self.assertEqual(derive_tier_from_permissions([{"permission": "edit-dataset"}]), "Build")

    def test_build_audit_records_classification(self):
        raw_members = [
            {
                "memberId": "m-1",
                "email": "alice@iterable.com",
                "firstName": "Alice",
                "lastName": "Departed",
                "memberType": "Act",
                "userKind": "internal",
                "isArchived": False,
                "isInactive": False,
            },
            {
                "memberId": "m-2",
                "email": "bob@iterable.com",
                "firstName": "Bob",
                "lastName": "Viewer",
                "memberType": "View",
                "userKind": "internal",
                "isArchived": False,
                "isInactive": False,
            },
            {
                "memberId": "m-3",
                "email": "carol@iterable.com",
                "firstName": "Carol",
                "lastName": "InactivePaid",
                "memberType": "Analyze",
                "userKind": "internal",
                "isArchived": False,
                "isInactive": True,
            },
            {
                "memberId": "m-4",
                "email": "dave@iterable.com",
                "firstName": "Dave",
                "lastName": "CleanOffboarded",
                "memberType": "View",
                "userKind": "internal",
                "isArchived": True,
                "isInactive": False,
            },
            {
                "memberId": "m-5",
                "email": "embed-service@iterable.com",
                "firstName": "Embed",
                "lastName": "Service",
                "memberType": "build",
                "userKind": "embed",
                "isArchived": False,
                "isInactive": False,
            },
            {
                "memberId": "m-6",
                "email": "eve@iterable.com",
                "firstName": "Eve",
                "lastName": "StillEmployed",
                "memberType": "admin",
                "userKind": "internal",
                "isArchived": False,
                "isInactive": False,
            },
        ]

        departed_workers = [
            DepartedWorker(email="alice@iterable.com", full_name="Alice Departed", termination_date="2026-08-01"),
            DepartedWorker(email="bob@iterable.com", full_name="Bob Viewer", termination_date="2026-07-15"),
            DepartedWorker(email="carol@iterable.com", full_name="Carol InactivePaid", termination_date="2026-06-01"),
            DepartedWorker(email="dave@iterable.com", full_name="Dave CleanOffboarded", termination_date="2026-05-01"),
        ]

        type_to_tier = {
            "Act": "Build",
            "View": "View",
            "Analyze": "Analyze",
            "build": "Build",
            "admin": "Build",
        }

        records = build_audit_records(raw_members, departed_workers, type_to_tier)
        by_email = {r.email: r for r in records}

        # Alice: departed, active, paid tier (Act maps to Build) -> CRITICAL
        alice = by_email["alice@iterable.com"]
        self.assertTrue(alice.is_departed)
        self.assertTrue(alice.is_paid)
        self.assertEqual(alice.license_type, "Build")
        self.assertEqual(alice.sigma_status, "active")
        self.assertEqual(alice.risk_level, "CRITICAL")
        self.assertEqual(alice.termination_date, "2026-08-01")

        # Bob: departed, active, free tier (View) -> HIGH
        bob = by_email["bob@iterable.com"]
        self.assertTrue(bob.is_departed)
        self.assertFalse(bob.is_paid)
        self.assertEqual(bob.license_type, "View")
        self.assertEqual(bob.sigma_status, "active")
        self.assertEqual(bob.risk_level, "HIGH")

        # Carol: departed, inactive, paid tier (Analyze) -> MEDIUM
        carol = by_email["carol@iterable.com"]
        self.assertTrue(carol.is_departed)
        self.assertTrue(carol.is_paid)
        self.assertEqual(carol.sigma_status, "inactive")
        self.assertEqual(carol.risk_level, "MEDIUM")

        # Dave: departed, deactivated, free -> CLEAN
        dave = by_email["dave@iterable.com"]
        self.assertTrue(dave.is_departed)
        self.assertFalse(dave.is_paid)
        self.assertEqual(dave.sigma_status, "deactivated")
        self.assertEqual(dave.risk_level, "CLEAN")

        # Embed user: userKind=embed is NOT counted as paid
        embed = by_email["embed-service@iterable.com"]
        self.assertFalse(embed.is_paid)

        # Eve: active employed user -> not departed
        eve = by_email["eve@iterable.com"]
        self.assertFalse(eve.is_departed)
        self.assertEqual(eve.risk_level, "LOW")

    def test_reporter_outputs(self):
        records = [
            MemberAuditRecord(
                email="alice@iterable.com",
                member_id="m-1",
                first_name="Alice",
                last_name="Departed",
                member_type="Act",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=True,
                termination_date="2026-08-01",
                adp_name="Alice Departed",
                is_paid=True,
                risk_level="CRITICAL",
            ),
            MemberAuditRecord(
                email="bob@iterable.com",
                member_id="m-2",
                first_name="Bob",
                last_name="Viewer",
                member_type="View",
                license_type="View",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=True,
                termination_date="2026-07-15",
                adp_name="Bob Viewer",
                is_paid=False,
                risk_level="HIGH",
            ),
        ]

        # Summary check
        summary = AuditReporter.render_summary(records)
        self.assertIn("SIGMA LICENSE & OFFBOARDING AUDIT", summary)
        self.assertIn("Active + Paid:    1", summary)
        self.assertIn("Active + Free:    1", summary)
        self.assertIn("TOTAL RECLAIMABLE:           1", summary)

        # Table check (verify alphabetical ordering by name)
        table = AuditReporter.render_table(list(reversed(records)))
        self.assertIn("alice@iterable.com", table)
        self.assertIn("CRITICAL", table)
        self.assertIn("bob@iterable.com", table)
        alice_idx = table.index("Alice Departed")
        bob_idx = table.index("Bob Viewer")
        self.assertLess(alice_idx, bob_idx)

        # JSON check
        json_str = AuditReporter.render_json(records)
        data = json.loads(json_str)
        self.assertEqual(data["summary"]["departedActivePaid"], 1)
        self.assertEqual(len(data["members"]), 2)

        # CSV check
        csv_str = AuditReporter.render_csv(records)
        self.assertIn("alice@iterable.com,Alice Departed,Build,Act,True,active,True,2026-08-01,CRITICAL", csv_str)

    def test_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "adp_cache.json"
            workers = [
                DepartedWorker(email="test1@iterable.com", full_name="Test One", termination_date="2026-01-01"),
                DepartedWorker(email="test2@iterable.com", full_name="Test Two", termination_date="2026-02-01"),
            ]
            SnowflakeWorkerStore.save_cache(workers, cache_file)
            loaded = SnowflakeWorkerStore.load_cached(cache_file, max_age_hours=24)
            self.assertEqual(loaded, workers)

    def test_remediation_dry_run_and_execution(self):
        owner_member = {
            "memberId": "owner-1",
            "email": "manager@iterable.com",
            "isArchived": False,
            "isInactive": False,
        }
        client = FakeSigmaClient(members=[owner_member])

        target = MemberAuditRecord(
            email="alice@iterable.com",
            member_id="m-1",
            first_name="Alice",
            last_name="Departed",
            member_type="Act",
            license_type="Build",
            user_kind="internal",
            sigma_status="active",
            is_archived=False,
            is_inactive=False,
            is_departed=True,
            is_paid=True,
            risk_level="CRITICAL",
        )

        # Dry-run: should not perform any mutation
        code = run_remediation_plan(
            client=client,
            targets=[target],
            downgrade=True,
            deactivate=True,
            new_owner_email="manager@iterable.com",
            archive_docs=False,
            execute=False,
            auto_confirm=True,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(client.updates), 0)
        self.assertEqual(len(client.deactivations), 0)

        # Live execution with auto_confirm
        code_exec = run_remediation_plan(
            client=client,
            targets=[target],
            downgrade=True,
            deactivate=True,
            new_owner_email="manager@iterable.com",
            archive_docs=False,
            execute=True,
            auto_confirm=True,
        )
        self.assertEqual(code_exec, 0)
        self.assertEqual(client.updates, [("m-1", "View")])
        self.assertEqual(client.deactivations, [("m-1", "owner-1", False)])

    def test_remediation_one_by_one(self):
        owner_member = {
            "memberId": "owner-1",
            "email": "manager@iterable.com",
            "isArchived": False,
            "isInactive": False,
        }
        client = FakeSigmaClient(members=[owner_member])

        target1 = MemberAuditRecord(
            email="user1@iterable.com",
            member_id="m-1",
            first_name="User",
            last_name="One",
            member_type="Act",
            license_type="Build",
            user_kind="internal",
            sigma_status="active",
            is_archived=False,
            is_inactive=False,
            is_departed=True,
            is_paid=True,
            risk_level="CRITICAL",
        )
        target2 = MemberAuditRecord(
            email="user2@iterable.com",
            member_id="m-2",
            first_name="User",
            last_name="Two",
            member_type="Analyze",
            license_type="Analyze",
            user_kind="internal",
            sigma_status="active",
            is_archived=False,
            is_inactive=False,
            is_departed=True,
            is_paid=True,
            risk_level="CRITICAL",
        )

        from unittest.mock import patch

        # Confirm user1 (y), skip user2 (n) in live mode
        with patch("builtins.input", side_effect=["y", "n"]):
            code = run_remediation_plan(
                client=client,
                targets=[target1, target2],
                downgrade=False,
                deactivate=True,
                new_owner_email="manager@iterable.com",
                archive_docs=False,
                execute=True,
                auto_confirm=True,
                one_by_one=True,
            )
            self.assertEqual(code, 0)
            self.assertEqual(client.deactivations, [("m-1", "owner-1", False)])
            self.assertEqual(target1.sigma_status, "deactivated")
            self.assertEqual(target2.sigma_status, "active")

    def test_activity_store_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "activity_cache.json"
            activities = {
                "builder@iterable.com": UserActivity(
                    email="builder@iterable.com",
                    last_activity="2026-10-08T12:00:00Z",
                    last_login="2026-10-08T09:00:00Z",
                    last_opened="2026-10-08T11:00:00Z",
                    last_edit="2026-10-08T12:00:00Z",
                    total_events=500,
                ),
                "viewer@iterable.com": UserActivity(
                    email="viewer@iterable.com",
                    last_activity="2026-10-07T15:00:00Z",
                    total_events=12,
                ),
            }
            ActivityStore.save_cache(activities, cache_file)
            loaded = ActivityStore.load_cached(cache_file)
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded["builder@iterable.com"].last_edit, "2026-10-08T12:00:00Z")
            self.assertEqual(loaded["builder@iterable.com"].total_events, 500)

    def test_utilization_classification(self):
        raw_members = [
            # 1. Active employee, Build tier, active editor -> ACTIVE_BUILDER
            {
                "memberId": "m-b1",
                "email": "active.builder@iterable.com",
                "firstName": "Active",
                "lastName": "Builder",
                "memberType": "admin",
                "userKind": "internal",
                "isArchived": False,
                "isInactive": False,
            },
            # 2. Active employee, Build tier, views only -> UNDERUTILIZED_BUILD
            {
                "memberId": "m-b2",
                "email": "viewing.builder@iterable.com",
                "firstName": "Viewing",
                "lastName": "Builder",
                "memberType": "build",
                "userKind": "internal",
                "isArchived": False,
                "isInactive": False,
            },
            # 3. Active employee, Analyze tier, active explorer -> ACTIVE_ANALYST
            {
                "memberId": "m-a1",
                "email": "active.analyst@iterable.com",
                "firstName": "Active",
                "lastName": "Analyst",
                "memberType": "Analyze",
                "userKind": "internal",
                "isArchived": False,
                "isInactive": False,
            },
            # 4. Active employee, paid tier, no events -> DORMANT
            {
                "memberId": "m-d1",
                "email": "dormant.user@iterable.com",
                "firstName": "Dormant",
                "lastName": "User",
                "memberType": "Analyze",
                "userKind": "internal",
                "isArchived": False,
                "isInactive": False,
            },
            # 5. Departed employee with paid tier -> DEPARTED
            {
                "memberId": "m-dep",
                "email": "departed.paid@iterable.com",
                "firstName": "Departed",
                "lastName": "Paid",
                "memberType": "build",
                "userKind": "internal",
                "isArchived": False,
                "isInactive": False,
            },
            # 6. Active employee on View tier -> FREE_VIEW
            {
                "memberId": "m-v1",
                "email": "free.view@iterable.com",
                "firstName": "Free",
                "lastName": "View",
                "memberType": "View",
                "userKind": "internal",
                "isArchived": False,
                "isInactive": False,
            },
        ]

        departed_workers = [
            DepartedWorker(email="departed.paid@iterable.com", full_name="Departed Paid", termination_date="2026-09-01"),
        ]

        activities = {
            "active.builder@iterable.com": UserActivity(
                email="active.builder@iterable.com",
                last_activity="2026-10-08T12:00:00Z",
                last_edit="2026-10-07T14:00:00Z",
                total_events=250,
            ),
            "viewing.builder@iterable.com": UserActivity(
                email="viewing.builder@iterable.com",
                last_activity="2026-10-08T10:00:00Z",
                last_edit="",  # Zero edits
                total_events=40,
            ),
            "active.analyst@iterable.com": UserActivity(
                email="active.analyst@iterable.com",
                last_activity="2026-10-06T09:00:00Z",
                total_events=80,
            ),
            # dormant.user@iterable.com is not in activities dict
        }

        type_to_tier = {
            "admin": "Build",
            "build": "Build",
            "Analyze": "Analyze",
            "View": "View",
        }

        records = build_audit_records(raw_members, departed_workers, type_to_tier, activities=activities)
        by_email = {r.email: r for r in records}

        # Active builder
        b1 = by_email["active.builder@iterable.com"]
        self.assertEqual(b1.utilization_status, "ACTIVE_BUILDER")
        self.assertEqual(b1.recommendation, "Keep (Active Builder)")
        self.assertEqual(b1.total_events, 250)

        # Viewing builder (underutilized)
        b2 = by_email["viewing.builder@iterable.com"]
        self.assertEqual(b2.utilization_status, "UNDERUTILIZED_BUILD")
        self.assertEqual(b2.recommendation, "Downgrade (View / Analyze)")
        self.assertEqual(b2.last_edit, "")

        # Active analyst
        a1 = by_email["active.analyst@iterable.com"]
        self.assertEqual(a1.utilization_status, "ACTIVE_ANALYST")
        self.assertEqual(a1.recommendation, "Keep (Active Analyst)")

        # Dormant user
        d1 = by_email["dormant.user@iterable.com"]
        self.assertEqual(d1.utilization_status, "DORMANT")
        self.assertEqual(d1.recommendation, "Reclaim (Downgrade to View)")
        self.assertEqual(d1.total_events, 0)

        # Departed user
        dep = by_email["departed.paid@iterable.com"]
        self.assertEqual(dep.utilization_status, "DEPARTED")
        self.assertEqual(dep.recommendation, "Deactivate")

        # Free view user
        v1 = by_email["free.view@iterable.com"]
        self.assertEqual(v1.utilization_status, "FREE_VIEW")
        self.assertEqual(v1.recommendation, "—")

    def test_utilization_table_and_summary_rendering(self):
        records = [
            MemberAuditRecord(
                email="builder@iterable.com",
                member_id="m-1",
                first_name="Active",
                last_name="Builder",
                member_type="build",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
                is_paid=True,
                risk_level="LOW",
                last_activity="2026-10-08T12:00:00Z",
                last_edit="2026-10-07T14:00:00Z",
                total_events=150,
                utilization_status="ACTIVE_BUILDER",
                recommendation="Keep (Active Builder)",
            ),
            MemberAuditRecord(
                email="viewbuilder@iterable.com",
                member_id="m-2",
                first_name="Viewing",
                last_name="Builder",
                member_type="build",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
                is_paid=True,
                risk_level="LOW",
                last_activity="2026-10-05T12:00:00Z",
                last_edit="",
                total_events=20,
                utilization_status="UNDERUTILIZED_BUILD",
                recommendation="Downgrade (View / Analyze)",
            ),
            MemberAuditRecord(
                email="dormant@iterable.com",
                member_id="m-3",
                first_name="Dormant",
                last_name="Analyst",
                member_type="Analyze",
                license_type="Analyze",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
                is_paid=True,
                risk_level="LOW",
                last_activity="",
                last_edit="",
                total_events=0,
                utilization_status="DORMANT",
                recommendation="Reclaim (Downgrade to View)",
            ),
        ]

        # Summary check
        summary = AuditReporter.render_summary(records)
        self.assertIn("Paid License Utilization & Optimization (Last 30 Days):", summary)
        self.assertIn("View-Only Builders:    1", summary)
        self.assertIn("Dormant Paid Seats:    1", summary)
        self.assertIn("Effectively Utilized:        1", summary)
        self.assertIn("TOTAL OPTIMIZATION TARGETS:          2", summary)

        # Utilization table check
        table = AuditReporter.render_table(records, utilization_mode=True)
        self.assertIn("#", table)
        self.assertIn("Utilization", table)
        self.assertIn("Recommendation", table)
        self.assertIn("Events", table)
        self.assertIn("Invited", table)
        self.assertIn("Days", table)
        self.assertIn("DORMANT", table)
        self.assertIn("UNDERUTILIZED_BUILD", table)
        self.assertIn("ACTIVE_BUILDER", table)
        # Verify DORMANT appears before ACTIVE_BUILDER (Never at top)
        dormant_pos = table.index("dormant@iterable.com")
        active_pos = table.index("builder@iterable.com")
        view_pos = table.index("viewbuilder@iterable.com")
        self.assertLess(dormant_pos, active_pos)
        self.assertLess(active_pos, view_pos)

        # JSON check
        json_data = json.loads(AuditReporter.render_json(records))
        util = json_data["summary"]["utilization"]
        self.assertEqual(util["activePaidTotal"], 3)
        self.assertEqual(util["activeBuilders"], 1)
        self.assertEqual(util["underutilizedBuilders"], 1)
        self.assertEqual(util["dormantPaid"], 1)

    def test_utilization_table_sorting(self):
        records = [
            MemberAuditRecord(
                email="zach.active@iterable.com",
                member_id="m-1",
                first_name="Zach",
                last_name="Active",
                member_type="build",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
                is_paid=True,
                risk_level="LOW",
                last_activity="2026-10-08T15:00:00Z",
                utilization_status="ACTIVE_BUILDER",
            ),
            MemberAuditRecord(
                email="alice.active@iterable.com",
                member_id="m-2",
                first_name="Alice",
                last_name="Active",
                member_type="build",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
                is_paid=True,
                risk_level="LOW",
                last_activity="2026-10-08T09:00:00Z",
                utilization_status="ACTIVE_BUILDER",
            ),
            MemberAuditRecord(
                email="bob.older@iterable.com",
                member_id="m-3",
                first_name="Bob",
                last_name="Older",
                member_type="build",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
                is_paid=True,
                risk_level="LOW",
                last_activity="2026-10-05T10:00:00Z",
                utilization_status="UNDERUTILIZED_BUILD",
            ),
            MemberAuditRecord(
                email="zoe.never@iterable.com",
                member_id="m-4",
                first_name="Zoe",
                last_name="Never",
                member_type="build",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
                is_paid=True,
                risk_level="LOW",
                last_activity="",
                utilization_status="DORMANT",
            ),
            MemberAuditRecord(
                email="adam.never@iterable.com",
                member_id="m-5",
                first_name="Adam",
                last_name="Never",
                member_type="build",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
                is_paid=True,
                risk_level="LOW",
                last_activity="Never",
                utilization_status="DORMANT",
            ),
        ]

        table = AuditReporter.render_table(records, utilization_mode=True)
        # Expected order:
        # 1. adam.never (Never, alpha email)
        # 2. zoe.never (Never, alpha email)
        # 3. alice.active (2026-10-08, alpha email)
        # 4. zach.active (2026-10-08, alpha email)
        # 5. bob.older (2026-10-05)
        pos_adam = table.index("adam.never@iterable.com")
        pos_zoe = table.index("zoe.never@iterable.com")
        pos_alice = table.index("alice.active@iterable.com")
        pos_zach = table.index("zach.active@iterable.com")
        pos_bob = table.index("bob.older@iterable.com")

        self.assertLess(pos_adam, pos_zoe)
        self.assertLess(pos_zoe, pos_alice)
        self.assertLess(pos_alice, pos_zach)
        self.assertLess(pos_zach, pos_bob)

    def test_braille_spinner_lifecycle(self):
        spinner = BrailleSpinner("Testing...")
        spinner.start()
        spinner.update("Updated...")
        spinner.stop()
        self.assertIsNone(spinner._thread)

    def test_wait_with_braille_stops_after_success_and_failure(self):
        events = []

        class FakeSpinner:
            def __init__(self, message):
                events.append(("init", message))

            def start(self):
                events.append(("start",))

            def stop(self):
                events.append(("stop",))

        result = MODULE.wait_with_braille(
            "Loading...",
            lambda: "done",
            spinner_factory=FakeSpinner,
        )
        self.assertEqual(result, "done")
        self.assertEqual(events, [("init", "Loading..."), ("start",), ("stop",)])

        events.clear()
        with self.assertRaisesRegex(RuntimeError, "failed"):
            MODULE.wait_with_braille(
                "Loading...",
                lambda: (_ for _ in ()).throw(RuntimeError("failed")),
                spinner_factory=FakeSpinner,
            )
        self.assertEqual(events, [("init", "Loading..."), ("start",), ("stop",)])

        events.clear()
        MODULE.wait_with_braille(
            "outer",
            lambda: MODULE.wait_with_braille("inner", lambda: None, spinner_factory=FakeSpinner),
            spinner_factory=FakeSpinner,
        )
        self.assertEqual(events, [("init", "outer"), ("start",), ("stop",)])

    def test_sigma_request_shows_braille_while_the_api_call_waits(self):
        messages = []

        def fake_wait(message, operation, **_kwargs):
            messages.append(message)
            return operation()

        client = MODULE.SigmaClient("https://example.test", "token")
        response = unittest.mock.Mock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__ = lambda self: response
        response.__exit__ = lambda self, *args: False
        with (
            unittest.mock.patch.object(MODULE, "wait_with_braille", fake_wait),
            unittest.mock.patch.object(MODULE.urllib.request, "urlopen", return_value=response),
        ):
            payload = client.request("GET", "/v2/members", params={"limit": 1})
        self.assertEqual(payload, {"ok": True})
        self.assertEqual(messages, ["Sigma GET /v2/members"])

    def test_fzf_pick_choice_fallback(self):
        from unittest.mock import patch
        entries = [("1", "1. Option One"), ("10", "10. Option Ten"), ("q", "q. Quit")]
        with patch("shutil.which", return_value=None), patch("builtins.input", return_value="10"):
            res = fzf_pick_choice(entries, prompt="pick> ", header="Choose")
            self.assertEqual(res, "10")

        with patch("shutil.which", return_value=None), patch("builtins.input", return_value="q"):
            res = fzf_pick_choice(entries, prompt="pick> ", header="Choose")
            self.assertEqual(res, "q")

    def test_fzf_pick_multi_fallback(self):
        from unittest.mock import patch
        entries = [("ALL", "ALL"), ("Act", "Act type"), ("build", "build type")]
        # Comma-separated choice
        with patch("shutil.which", return_value=None), patch("builtins.input", return_value="2,3"):
            res = fzf_pick_multi(entries, prompt="pick> ", header="Choose")
            self.assertEqual(res, ["Act", "build"])

        # 'all' choice
        with patch("shutil.which", return_value=None), patch("builtins.input", return_value="all"):
            res = fzf_pick_multi(entries, prompt="pick> ", header="Choose")
            self.assertEqual(res, ["ALL", "Act", "build"])

        # cancel / empty
        with patch("shutil.which", return_value=None), patch("builtins.input", return_value=""):
            res = fzf_pick_multi(entries, prompt="pick> ", header="Choose")
            self.assertEqual(res, [])

    def test_fzf_pick_multi_can_clear_search_after_each_selection(self):
        completed = unittest.mock.Mock(returncode=0, stdout="a@example.com\tA\n")
        with (
            unittest.mock.patch("shutil.which", return_value="/opt/homebrew/bin/fzf"),
            unittest.mock.patch.object(MODULE.sys.stdin, "isatty", return_value=True),
            unittest.mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run,
        ):
            selected = fzf_pick_multi(
                [("a@example.com", "A")],
                clear_query_on_toggle=True,
            )
        self.assertEqual(selected, ["a@example.com"])
        command = run.call_args.args[0]
        self.assertIn(
            "--bind=tab:toggle+clear-query,ctrl-a:select-all,ctrl-d:deselect-all",
            command,
        )

    def test_render_table_contains_type_column(self):
        records = [
            MemberAuditRecord(
                email="andre.gomes@iterable.com",
                member_id="m-1",
                first_name="Andre",
                last_name="Gomes",
                member_type="Act",
                license_type="Build",
                user_kind="internal",
                sigma_status="active",
                is_archived=False,
                is_inactive=False,
                is_departed=False,
                is_paid=True,
                risk_level="LOW",
                last_activity="2026-10-08T12:00:00Z",
                utilization_status="UNDERUTILIZED_BUILD",
                recommendation="Downgrade (View / Analyze)",
            )
        ]
        table = AuditReporter.render_table(records, utilization_mode=True)
        self.assertIn("Type", table)
        self.assertIn("Tier", table)
        self.assertIn("Build", table)
        self.assertIn("Act", table)


if __name__ == "__main__":
    unittest.main()
