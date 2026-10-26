import unittest

from tests.base import app_module  # noqa: F401  (sets up paths first)
from waste_finder import find_waste_item


class ClassifierTests(unittest.TestCase):
    def check(self, text, category, bin_contains=None):
        r = find_waste_item(text)
        self.assertEqual(r["status"], "found", f"{text!r} -> {r}")
        self.assertEqual(r["category"], category, f"{text!r} -> {r['item_name']}")
        if bin_contains:
            self.assertIn(bin_contains, r["bin"])

    def test_common_items(self):
        cases = {
            "banana peel": "Wet waste",
            "banana skin": "Wet waste",
            "eggshell": "Wet waste",
            "old newspaper": "Dry waste",
            "plastic bottle": "Dry waste",
            "aluminum can": "Dry waste",
            "broken glass": "Dry waste",
            "smartphone": "E-waste",
            "mobile charger": "E-waste",
            "dead battery": "Special care waste",
            "tube light": "Special care waste",
            "used syringe": "Special care waste",
            "sanitary pad": "Sanitary waste",
            "diaper": "Sanitary waste",
        }
        for text, category in cases.items():
            with self.subTest(text):
                self.check(text, category)

    def test_typos_are_corrected(self):
        self.check("banan peel", "Wet waste")
        self.check("plasitc bottle", "Dry waste")

    def test_hazardous_words_win(self):
        # Safety first: a dangerous word must never be sent to the dry bin.
        for text in ("old battery pack", "expired medicine strip", "needle"):
            with self.subTest(text):
                r = find_waste_item(text)
                self.assertEqual(r["status"], "found")
                self.assertEqual(r["category"], "Special care waste")

    def test_ambiguous_items_ask_which_one(self):
        for text in ("water bottle", "bulb", "pizza box"):
            with self.subTest(text):
                r = find_waste_item(text)
                self.assertEqual(r["status"], "ambiguous")
                self.assertGreaterEqual(len(r["candidates"]), 2)

    def test_unknown_item_asks_guided_questions(self):
        r = find_waste_item("door")
        self.assertEqual(r["status"], "ask")
        self.assertTrue(r["questions"])

    def test_empty_and_odd_input(self):
        for text in ("", "   ", None, 123):
            with self.subTest(repr(text)):
                self.assertEqual(find_waste_item(text)["status"], "empty")
        # long and script-like input must not crash
        self.assertIn(find_waste_item("a" * 500)["status"], ("ask", "found"))
        self.assertIn(find_waste_item("<script>alert(1)</script>")["status"], ("ask", "found"))

    def test_electronic_scrap_alias_is_unique(self):
        r = find_waste_item("electronic scrap")
        self.assertEqual(r["item_name"], "Electronic scrap")


class UnknownLogTests(unittest.TestCase):
    def test_log_is_capped_and_cleaned(self):
        import os
        import tempfile
        from classifier import Classifier, MAX_LOG_BYTES
        from waste_finder import DATA_FILE

        path = os.path.join(tempfile.mkdtemp(), "log.txt")
        clf = Classifier(str(DATA_FILE), unknown_log=path)

        clf._log("line one\nline two\tTabbed")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("\n"), 1)            # newlines in input removed
        self.assertNotIn("\tTabbed", text)               # tab in input removed

        clf._log("x" * 1000)
        with open(path, encoding="utf-8") as f:
            self.assertLessEqual(max(len(line) for line in f), 140)

        with open(path, "a", encoding="utf-8") as f:     # pretend the file is full
            f.write("." * MAX_LOG_BYTES)
        size = os.path.getsize(path)
        clf._log("one more")
        self.assertEqual(os.path.getsize(path), size)    # nothing was added


if __name__ == "__main__":
    unittest.main()
