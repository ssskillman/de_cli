import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "sigma_search_workbooks.py"
SPEC = importlib.util.spec_from_file_location("sigma_search_workbooks", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class NameMatchTests(unittest.TestCase):
    def test_plain_text_matches_anywhere_ignoring_case(self):
        self.assertTrue(MODULE.name_matches("Deliverability Team Dashboard", "team"))
        self.assertTrue(MODULE.name_matches("Customer Health", "HEALTH"))
        self.assertFalse(MODULE.name_matches("Revenue", "health"))

    def test_star_and_question_wildcards(self):
        self.assertTrue(MODULE.name_matches("Deliverability Team Dashboard", "deliver*"))
        self.assertTrue(MODULE.name_matches("Deliverability Team Dashboard", "*dashboard"))
        self.assertTrue(MODULE.name_matches("abc", "a?c"))
        self.assertFalse(MODULE.name_matches("ac", "a?c"))
        self.assertFalse(MODULE.name_matches("Deliverability Team Dashboard", "revenue*"))

    def test_literal_brackets_in_plain_search(self):
        self.assertTrue(MODULE.name_matches("Score [FY26]", "[fy26]"))

    def test_empty_pattern_rejected(self):
        with self.assertRaises(ValueError):
            MODULE.name_matches("Anything", "   ")

    def test_search_filters_and_sorts(self):
        rows = [
            {"name": "Zebra", "workbookId": "z"},
            {"name": "Alpha Health", "workbookId": "a"},
            {"name": "Other", "workbookId": "o"},
        ]
        found = MODULE.search_workbooks(rows, "health")
        self.assertEqual([row["workbookId"] for row in found], ["a"])

        found = MODULE.search_workbooks(rows, "*a*")
        self.assertEqual([row["name"] for row in found], ["Alpha Health", "Zebra"])


if __name__ == "__main__":
    unittest.main()
