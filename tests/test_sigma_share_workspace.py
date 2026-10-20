import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "sigma_share_workspace.py"
SPEC = importlib.util.spec_from_file_location("sigma_share_workspace", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class FakeApi:
    def __init__(self):
        self.workspaces = [
            {
                "workspaceId": "ws-1",
                "inodeId": "ws-1",
                "name": MODULE.WORKSPACE_NAME,
            }
        ]
        self.folders = []
        self.grants = []
        self.calls = []
        self._seq = 0
        parent = "ws-1"
        for name, children in MODULE.TREE:
            self._add_folder(parent, name, children)

    def _add_folder(self, parent_id, name, children):
        self._seq += 1
        folder_id = f"folder-{self._seq}"
        self.folders.append(
            {"id": folder_id, "name": name, "parentId": parent_id, "type": "folder"}
        )
        for child_name, grandchildren in children:
            self._add_folder(folder_id, child_name, grandchildren)

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, params, body))
        if method == "GET" and path == "/v2.1/workspaces":
            return {"entries": list(self.workspaces)}
        if method == "GET" and path == "/v2/files":
            parent_id = (params or {}).get("parentId")
            rows = [
                row
                for row in self.folders
                if row.get("parentId") == parent_id and row.get("type") == "folder"
            ]
            return {"entries": rows}
        if method == "GET" and path == "/v2/grants":
            inode_id = (params or {}).get("inodeId")
            rows = [row for row in self.grants if row.get("inodeId") == inode_id]
            return {"entries": rows}
        if method == "POST" and path == "/v2/workspaces/ws-1/grants":
            for grant in body["grants"]:
                grantee = grant["grantee"]
                self.grants.append(
                    {
                        "inodeId": "ws-1",
                        "teamId": grantee.get("teamId"),
                        "memberId": grantee.get("memberId"),
                        "permission": grant["permission"],
                    }
                )
            return {}
        if method == "POST" and path == "/v2/grants":
            grantee = body["grantee"]
            self.grants.append(
                {
                    "inodeId": body["inodeId"],
                    "teamId": grantee.get("teamId"),
                    "memberId": grantee.get("memberId"),
                    "permission": body["permission"],
                }
            )
            return {}
        raise AssertionError(f"unexpected {method} {path}")

    def paginate(self, path, params):
        response = self.request("GET", path, params)
        return response.get("entries") or []


def posts(api):
    return [(path, body) for method, path, _params, body in api.calls if method == "POST"]


class ShareWorkspaceTests(unittest.TestCase):
    def test_node_order_is_workspace_then_tree(self):
        api = FakeApi()
        paths = [node.path for node in MODULE.iter_nodes(api)]
        self.assertEqual(paths[0], MODULE.WORKSPACE_NAME)
        self.assertEqual(paths[1], f"{MODULE.WORKSPACE_NAME} / 01 - Certified Data Models")
        self.assertEqual(
            paths[2],
            f"{MODULE.WORKSPACE_NAME} / 01 - Certified Data Models / ARR & Revenue",
        )
        self.assertEqual(len(paths), 14)
        self.assertTrue(paths[-1].endswith("99 - Archive"))

    def test_all_members_posts_workspace_and_folder_grants(self):
        api = FakeApi()
        nodes = MODULE.iter_nodes(api)
        choice = MODULE.ShareChoice([("team", MODULE.ALL_MEMBERS_TEAM_ID)], "explore")

        def choose(node, _grants):
            if node.path.endswith("02 - Development & QA"):
                return None
            return choice

        lines = MODULE.review(api, nodes, choose, lambda _node, _choice: "apply")
        workspace_posts = [body for path, body in posts(api) if path.startswith("/v2/workspaces/")]
        folder_posts = [body for path, body in posts(api) if path == "/v2/grants"]
        self.assertEqual(len(workspace_posts), 1)
        self.assertEqual(
            workspace_posts[0]["grants"][0]["grantee"],
            {"teamId": MODULE.ALL_MEMBERS_TEAM_ID},
        )
        self.assertEqual(workspace_posts[0]["grants"][0]["permission"], "explore")
        self.assertEqual(len(folder_posts), 12)
        self.assertTrue(all(body["permission"] == "explore" for body in folder_posts))
        self.assertTrue(any(line.startswith("skipped\t") and line.endswith("02 - Development & QA") for line in lines))
        skipped = next(node for node in nodes if node.path.endswith("02 - Development & QA"))
        self.assertFalse(any(body["inodeId"] == skipped.inode_id for body in folder_posts))

    def test_cancel_and_quit_do_not_share_the_rest(self):
        api = FakeApi()
        nodes = MODULE.iter_nodes(api)
        choice = MODULE.ShareChoice([("member", "member-1")], "view")

        def confirm(node, _choice):
            if node.kind == "workspace":
                return "skip"
            return "quit"

        lines = MODULE.review(api, nodes, lambda _node, _grants: choice, confirm)
        self.assertEqual(posts(api), [])
        self.assertEqual(lines[0], f"skipped\t{MODULE.WORKSPACE_NAME}")
        self.assertTrue(lines[1].startswith("stopped\t"))
        self.assertEqual(len(lines), 2)

    def test_existing_grant_is_not_posted_again_or_downgraded(self):
        api = FakeApi()
        nodes = MODULE.iter_nodes(api)
        folder = next(node for node in nodes if node.path.endswith("01 - Certified Data Models"))
        api.grants.append(
            {
                "inodeId": folder.inode_id,
                "teamId": MODULE.ALL_MEMBERS_TEAM_ID,
                "memberId": None,
                "permission": "explore",
            }
        )
        same = MODULE.ShareChoice([("team", MODULE.ALL_MEMBERS_TEAM_ID)], "explore")
        lower = MODULE.ShareChoice([("team", MODULE.ALL_MEMBERS_TEAM_ID)], "view")
        lines = MODULE.apply_grants(api, folder, same)
        self.assertEqual(posts(api), [])
        self.assertTrue(lines[0].startswith("already\t"))
        lines = MODULE.apply_grants(api, folder, lower)
        self.assertEqual(posts(api), [])
        self.assertTrue(lines[0].startswith("already\t"))

    def test_all_members_label_is_not_a_normal_team_name(self):
        label = MODULE.grantee_label("team", MODULE.ALL_MEMBERS_TEAM_ID, {}, {})
        self.assertEqual(label, "All members of the organization")


if __name__ == "__main__":
    unittest.main()
