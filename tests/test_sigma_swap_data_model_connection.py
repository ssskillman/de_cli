import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "scripts" / "sigma_swap_data_model_connection.py"
SPEC = importlib.util.spec_from_file_location("sigma_swap_data_model_connection", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class FakeApi:
    def __init__(self):
        self.calls = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/v2/files/"):
            return {"ownerId": "new-member", "type": "dataset", "name": "Orders"}
        return {"ok": True}


class MatchTests(unittest.TestCase):
    def test_duplicate_names_stay_distinct(self):
        rows = [
            {"dataModelId": "aaa", "name": "Account_Bindings"},
            {"dataModelId": "bbb", "name": "Account_Bindings"},
        ]
        with self.assertRaises(ValueError):
            MODULE.unique_match(rows, "Account_Bindings", "dataModelId")
        found = MODULE.unique_match(rows, "bbb", "dataModelId")
        self.assertEqual(found["dataModelId"], "bbb")

    def test_fzf_wraps_and_still_filters_by_typing(self):
        command = MODULE.fzf_command(header="Assets", prompt="asset> ")
        self.assertIn(MODULE.FZF_WRAP_UP, command)
        self.assertIn(MODULE.FZF_WRAP_DOWN, command)
        self.assertIn("--with-nth=2", command)
        self.assertNotIn("--disabled", command)


class SwapBodyTests(unittest.TestCase):
    def test_whole_model_swaps_tables_without_a_path_limit(self):
        body, warnings = MODULE.build_swap_request(
            scope="whole",
            source=None,
            sources=[],
            from_id="old",
            to_id="new",
        )
        self.assertEqual(
            body,
            {
                "connectionMapping": [
                    {"fromId": "old", "toId": "new", "swapTables": True}
                ]
            },
        )
        self.assertEqual(warnings, [])

    def test_table_swap_is_limited_to_that_path(self):
        body, warnings = MODULE.build_swap_request(
            scope="source",
            source={"type": "table", "tableId": "t1"},
            sources=[],
            from_id="old",
            to_id="new",
            table_path=["ANALYTICS", "BI", "ORDERS"],
        )
        mapping = body["connectionMapping"][0]
        self.assertTrue(mapping["swapTables"])
        self.assertEqual(
            mapping["paths"],
            [
                {
                    "fromPath": ["ANALYTICS", "BI", "ORDERS"],
                    "toPath": ["ANALYTICS", "BI", "ORDERS"],
                }
            ],
        )
        self.assertEqual(warnings, [])

    def test_custom_sql_warns_when_several_share_the_connection(self):
        sources = [
            {"type": "custom-sql", "customSqlId": "a", "connectionId": "old"},
            {"type": "custom-sql", "customSqlId": "b", "connectionId": "old"},
        ]
        body, warnings = MODULE.build_swap_request(
            scope="source",
            source=sources[0],
            sources=sources,
            from_id="old",
            to_id="new",
        )
        self.assertFalse(body["connectionMapping"][0]["swapTables"])
        self.assertIn("2 custom SQL sources", warnings[0])

    def test_dataset_source_is_not_a_connection_swap(self):
        with self.assertRaises(ValueError) as caught:
            MODULE.build_swap_request(
                scope="source",
                source={"type": "dataset", "datasetId": "d1"},
                sources=[],
                from_id="old",
                to_id="new",
            )
        self.assertIn("replacement source id", str(caught.exception))

    def test_same_connection_is_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.build_swap_request(
                scope="whole",
                source=None,
                sources=[],
                from_id="same",
                to_id="same",
            )


class OwnerTests(unittest.TestCase):
    def test_owner_body_is_owner_id_only(self):
        self.assertEqual(MODULE.ownership_body("member-2"), {"ownerId": "member-2"})

    def test_owner_accepts_one_model_or_dataset(self):
        models = MODULE.model_assets(
            [{"dataModelId": "m1", "name": "Revenue", "path": "BI", "ownerId": "old"}]
        )
        datasets = MODULE.dataset_assets(
            [{"datasetId": "d1", "name": "Orders", "path": "BI", "owner": "old"}]
        )
        self.assertEqual(models[0]["kind"], "data-model")
        self.assertEqual(datasets[0]["kind"], "dataset")
        self.assertEqual(MODULE.validate_new_owner(
            {"memberId": "member-2"}, "old"
        ), "member-2")

    def test_same_or_inactive_owner_is_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.validate_new_owner({"memberId": "old"}, "old")
        with self.assertRaises(ValueError):
            MODULE.validate_new_owner(
                {"memberId": "new", "isInactive": True}, "old"
            )

    def test_preview_and_cancel_do_not_write(self):
        api = FakeApi()
        args = SimpleNamespace(execute=False, yes=False)
        with patch("builtins.input", return_value="n"), patch(
            "sys.stdin.isatty", return_value=True
        ):
            code = MODULE._maybe_apply(args, lambda: api.request("POST", "/nope"), "Apply? ")
        self.assertEqual(code, 0)
        self.assertEqual(api.calls, [])

    def test_apply_owner_patches_then_verifies(self):
        api = FakeApi()
        verified = MODULE.apply_owner(api, "d1", "new-member")
        self.assertEqual(api.calls[0], ("PATCH", "/v2/files/d1", {"ownerId": "new-member"}))
        self.assertEqual(api.calls[1][0], "GET")
        self.assertEqual(verified["ownerId"], "new-member")


if __name__ == "__main__":
    unittest.main()
