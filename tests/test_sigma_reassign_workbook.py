import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "sigma_reassign_workbook.py"
SPEC = importlib.util.spec_from_file_location("sigma_reassign_workbook", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class FakeApi:
    def __init__(self, workbook):
        self.workbook = dict(workbook)
        self.calls = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if method == "PATCH":
            self.workbook.update(body or {})
        return dict(self.workbook)


class LookupApi:
    def __init__(self):
        self.calls = []

    def paginate(self, path, params):
        self.calls.append(("PAGINATE", path, params))
        return [
            {
                "workbookId": "9f17d4bb-1598-4d14-b7c4-8f1f87322486",
                "workbookUrlId": "6EeiHH2KyCnS2JyyqyFJxu",
                "name": "Deliverability Team Dashboard",
            }
        ]

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        return {
            "id": path.rsplit("/", 1)[-1],
            "type": "workbook",
            "name": "Deliverability Team Dashboard",
            "ownerId": "owner-1",
        }


class ReassignWorkbookTests(unittest.TestCase):
    def test_resolves_sigma_url_id_to_workbook_inode(self):
        api = LookupApi()
        url = (
            "https://app.sigmacomputing.com/iterable/workbook/"
            "Deliverability-Team-Dashboard-6EeiHH2KyCns2JyyqyFJxu"
        )

        workbook = MODULE.resolve_workbook_reference(api, url)

        self.assertEqual(
            workbook["id"], "9f17d4bb-1598-4d14-b7c4-8f1f87322486"
        )
        self.assertEqual(api.calls[-1][0:2], ("GET", "/v2/files/9f17d4bb-1598-4d14-b7c4-8f1f87322486"))

    def test_reassigns_one_workbook_and_verifies_owner(self):
        api = FakeApi(
            {
                "id": "workbook-1",
                "name": "Pipeline",
                "type": "workbook",
                "ownerId": "old-owner",
            }
        )

        result = MODULE.reassign_workbook(api, "workbook-1", "new-owner")

        self.assertEqual(result["ownerId"], "new-owner")
        self.assertEqual(
            api.calls,
            [
                ("GET", "/v2/files/workbook-1", None),
                ("PATCH", "/v2/files/workbook-1", {"ownerId": "new-owner"}),
                ("GET", "/v2/files/workbook-1", None),
            ],
        )

    def test_refuses_non_workbook_inode(self):
        api = FakeApi(
            {"id": "folder-1", "name": "Folder", "type": "folder", "ownerId": "old"}
        )

        with self.assertRaisesRegex(ValueError, "not a workbook"):
            MODULE.reassign_workbook(api, "folder-1", "new-owner")

        self.assertEqual(api.calls, [("GET", "/v2/files/folder-1", None)])


if __name__ == "__main__":
    unittest.main()
