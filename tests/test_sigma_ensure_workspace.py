import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "sigma_ensure_workspace.py"
SPEC = importlib.util.spec_from_file_location("sigma_ensure_workspace", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class FakeApi:
    def __init__(self, workspaces=None, folders=None):
        self.workspaces = list(workspaces or [])
        self.folders = list(folders or [])
        self.calls = []
        self._seq = 0

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        if method == "GET" and path == "/v2.1/workspaces":
            name = (params or {}).get("exactName", "")
            matches = [
                row
                for row in self.workspaces
                if str(row.get("name") or "").casefold() == name.casefold()
            ]
            return {"entries": matches}
        if method == "POST" and path == "/v2/workspaces":
            self._seq += 1
            created = {
                "workspaceId": f"ws-{self._seq}",
                "inodeId": f"inode-ws-{self._seq}",
                "name": body["name"],
            }
            self.workspaces.append(created)
            return created
        if method == "GET" and path == "/v2/files":
            parent_id = (params or {}).get("parentId")
            rows = [
                row
                for row in self.folders
                if row.get("parentId") == parent_id and row.get("type") == "folder"
            ]
            return {"entries": rows, "nextPage": None}
        if method == "POST" and path == "/v2/files":
            self._seq += 1
            created = {
                "id": f"folder-{self._seq}",
                "name": body["name"],
                "parentId": body["parentId"],
                "type": "folder",
            }
            self.folders.append(created)
            return created
        raise AssertionError(f"unexpected {method} {path}")

    def paginate(self, path, params):
        response = self.request("GET", path, params)
        return response.get("entries") or response.get("workspaces") or []


def posts(api):
    return [(path, body) for method, path, _params, body in api.calls if method == "POST"]


class EnsureWorkspaceTests(unittest.TestCase):
    def test_nothing_exists_creates_workspace_and_all_folders(self):
        api = FakeApi()
        lines = MODULE.ensure_tree(api)

        self.assertEqual(len(posts(api)), 14)
        self.assertEqual(
            posts(api)[0],
            ("/v2/workspaces", {"name": MODULE.WORKSPACE_NAME, "noDuplicates": True}),
        )
        folder_posts = [body for path, body in posts(api) if path == "/v2/files"]
        self.assertEqual(len(folder_posts), 13)
        self.assertTrue(all(body["type"] == "folder" for body in folder_posts))

        by_parent = {}
        for body in folder_posts:
            by_parent.setdefault(body["parentId"], []).append(body["name"])

        self.assertEqual(
            by_parent["inode-ws-1"],
            [
                "01 - Certified Data Models",
                "02 - Development & QA",
                "03 - Shared Workbooks",
                "04 - Department Analytics",
                "99 - Archive",
            ],
        )
        certified_id = next(
            row["id"] for row in api.folders if row["name"] == "01 - Certified Data Models"
        )
        self.assertEqual(
            by_parent[certified_id],
            [
                "ARR & Revenue",
                "Customers & Accounts",
                "Pipeline & Forecasting",
                "Product & Usage",
                "Finance & Operations",
            ],
        )
        department_id = next(
            row["id"] for row in api.folders if row["name"] == "04 - Department Analytics"
        )
        self.assertEqual(
            by_parent[department_id],
            [
                "GTM / Sales / Marketing",
                "Customer Success",
                "Finance / Product / People",
            ],
        )
        self.assertEqual(len(lines), 14)
        self.assertTrue(all(line.startswith("created\t") for line in lines))

    def test_partial_tree_creates_only_missing_folders(self):
        api = FakeApi(
            workspaces=[
                {
                    "workspaceId": "ws-existing",
                    "inodeId": "inode-existing",
                    "name": MODULE.WORKSPACE_NAME,
                }
            ],
            folders=[
                {
                    "id": "folder-01",
                    "name": "01 - certified data models",
                    "parentId": "inode-existing",
                    "type": "folder",
                },
                {
                    "id": "folder-arr",
                    "name": "ARR & Revenue",
                    "parentId": "folder-01",
                    "type": "folder",
                },
            ],
        )
        lines = MODULE.ensure_tree(api)
        created_names = [body["name"] for path, body in posts(api) if path == "/v2/files"]
        self.assertFalse(any(path == "/v2/workspaces" for path, _body in posts(api)))
        self.assertNotIn("01 - Certified Data Models", created_names)
        self.assertNotIn("ARR & Revenue", created_names)
        self.assertIn("Customers & Accounts", created_names)
        self.assertIn("02 - Development & QA", created_names)
        self.assertEqual(len(created_names), 11)
        self.assertTrue(any(line.startswith("exists\t") and "ARR & Revenue" in line for line in lines))
        customers = next(body for _path, body in posts(api) if body.get("name") == "Customers & Accounts")
        self.assertEqual(customers["parentId"], "folder-01")

    def test_complete_tree_makes_no_posts(self):
        api = FakeApi()
        MODULE.ensure_tree(api)
        again = FakeApi(workspaces=list(api.workspaces), folders=list(api.folders))
        lines = MODULE.ensure_tree(again)
        self.assertEqual(posts(again), [])
        self.assertEqual(len(lines), 14)
        self.assertTrue(all(line.startswith("exists\t") for line in lines))

    def test_duplicate_workspace_stops_without_creating(self):
        api = FakeApi(
            workspaces=[
                {"workspaceId": "a", "inodeId": "ia", "name": MODULE.WORKSPACE_NAME},
                {"workspaceId": "b", "inodeId": "ib", "name": MODULE.WORKSPACE_NAME},
            ]
        )
        with self.assertRaises(MODULE.WorkspaceError):
            MODULE.ensure_tree(api)
        self.assertEqual(posts(api), [])


if __name__ == "__main__":
    unittest.main()
