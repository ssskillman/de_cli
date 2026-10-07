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
MemberAuditRecord = MODULE.MemberAuditRecord
derive_tier_from_permissions = MODULE.derive_tier_from_permissions
build_audit_records = MODULE.build_audit_records
AuditReporter = MODULE.AuditReporter
SnowflakeWorkerStore = MODULE.SnowflakeWorkerStore
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
        self.assertIn("alice@iterable.com,Alice Departed,Build,Act,True,active,True,2026-08-01,CRITICAL,m-1", csv_str)

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


if __name__ == "__main__":
    unittest.main()
