import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "sigma_reactivate_user.py"
SPEC = importlib.util.spec_from_file_location("sigma_reactivate_user", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def member(**kwargs):
    defaults = {
        "memberId": "m-1",
        "email": "ada@iterable.com",
        "isArchived": True,
        "isInactive": False,
    }
    defaults.update(kwargs)
    return defaults


class ReactivateTests(unittest.TestCase):
    def test_archived_member_is_unarchived(self):
        self.assertEqual(MODULE.reactivation_body(member()), {"isArchived": False})

    def test_active_member_needs_no_change(self):
        self.assertEqual(MODULE.reactivation_body(member(isArchived=False)), {})

    def test_scim_inactive_requires_force(self):
        inactive = member(isArchived=False, isInactive=True)
        with self.assertRaises(MODULE.ReactivateError):
            MODULE.reactivation_body(inactive)
        self.assertEqual(
            MODULE.reactivation_body(inactive, force=True),
            {"isInactive": False},
        )

    def test_archived_and_inactive_clears_both_when_forced(self):
        self.assertEqual(
            MODULE.reactivation_body(member(isInactive=True), force=True),
            {"isArchived": False, "isInactive": False},
        )

    def test_email_must_match_one_member(self):
        members = [
            member(),
            member(memberId="m-2", email="grace@iterable.com"),
        ]
        found = MODULE.find_member(members, "Ada@iterable.com")
        self.assertEqual(found["memberId"], "m-1")
        with self.assertRaises(MODULE.ReactivateError):
            MODULE.find_member(members, "missing@iterable.com")
        with self.assertRaises(MODULE.ReactivateError):
            MODULE.find_member(members + [member(memberId="m-3")], "ada@iterable.com")

    def test_apply_patches_then_checks_the_response(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def request(self, method, path, params=None, body=None):
                self.calls.append((method, path, body))
                return {"email": "ada@iterable.com", "isArchived": False, "isInactive": False}

        client = FakeClient()
        line = MODULE.apply_reactivation(client, member(), {"isArchived": False})
        self.assertEqual(
            client.calls,
            [("PATCH", "/v2/members/m-1", {"isArchived": False})],
        )
        self.assertIn("reactivated ada@iterable.com", line)

    def test_apply_rejects_a_response_that_is_still_archived(self):
        class FakeClient:
            def request(self, method, path, params=None, body=None):
                return {"isArchived": True}

        with self.assertRaises(MODULE.ReactivateError):
            MODULE.apply_reactivation(FakeClient(), member(), {"isArchived": False})


if __name__ == "__main__":
    unittest.main()
