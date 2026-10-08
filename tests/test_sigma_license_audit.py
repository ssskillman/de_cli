import importlib.util
import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
