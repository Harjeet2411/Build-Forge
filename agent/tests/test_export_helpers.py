"""export.py helpers: durations, timestamps, and the CSV formula guard."""
from __future__ import annotations

import unittest

from buildforge_agent.export import _csv_safe, fmt_dur, fmt_time, to_csv


class FormatTest(unittest.TestCase):
    def test_duration_units(self):
        cases = {None: "", 0: "0 s", 89: "89 s", 90: "1 min", 5399: "89 min", 5400: "1.5 h",
                 172799: "48.0 h", 172800: "2.0 days"}
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(fmt_dur(seconds), expected)

    def test_time_uses_the_requested_offset(self):
        ts = 1_767_225_600  # 2026-01-01 00:00 UTC
        self.assertEqual(fmt_time(ts, 0), "2026-01-01 00:00")
        self.assertEqual(fmt_time(ts, 19800), "2026-01-01 05:30")      # IST
        self.assertEqual(fmt_time(ts, -18000, with_time=False), "2025-12-31")
        self.assertEqual(fmt_time(None, 0), "")
        self.assertEqual(fmt_time(0, 0), "")


class CsvTest(unittest.TestCase):
    def test_formula_guard(self):
        for text in ("=SUM(A1)", "+1", "-1", "@cmd", "\tx", "\rx"):
            with self.subTest(text=text):
                self.assertEqual(_csv_safe(text), "'" + text)
        for value in ("plain", "a=b", "", 5, None):
            self.assertEqual(_csv_safe(value), value)

    def test_filters_and_rows_are_guarded_in_the_file(self):
        data = to_csv("Report", [("Search", "=HYPERLINK(1)")], None, {
            "kpis": [("Failures", 3)], "per_label": "Day", "categories": [], "per": [],
            "errors": [["@evil", 1, 1, 0, "n/a"]], "pipelines": []}, 0)
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        text = data.decode("utf-8-sig")
        self.assertIn("Search,'=HYPERLINK(1)", text)
        self.assertIn("'@evil,1,1,0,n/a", text)


if __name__ == "__main__":
    unittest.main()
