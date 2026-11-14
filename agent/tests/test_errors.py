"""errors.py: log cleaning, stage list, fingerprints and the trimming of long output."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildforge_agent import errors  # noqa: E402

PIPELINE = """\
[Pipeline] { (Checkout)
+ git fetch
[Pipeline] { (Build)
[Pipeline] sh
+ make
make: *** [all] Error 2
[Pipeline] }
ERROR: script returned exit code 2
Stage "Deploy" skipped due to earlier failure
[Pipeline] { (Deploy)
[Pipeline] { (Notify)
Finished: FAILURE
"""


class CleanLogTest(unittest.TestCase):
    def test_strips_colour_codes_timestamps_and_jenkins_notes(self):
        raw = ("2026-11-14T10:00:01.123Z \x1b[31mred text\x1b[0m\n"
               "[2026-11-14 10:00:02] plain\n"
               "\x1b[8mha:AAAA\x1b[0mvisible\n")
        self.assertEqual(errors.clean_log(raw), "red text\nplain\nvisible")


class StagesTest(unittest.TestCase):
    def test_marks_failed_skipped_and_unknown_stages(self):
        got = {s["name"]: s["status"] for s in errors.stages(PIPELINE, failed="Build")}
        self.assertEqual(got, {"Checkout": "passed", "Build": "failed", "Deploy": "skipped", "Notify": "unknown"})

    def test_repeated_stage_names_are_listed_once(self):
        log = "[Pipeline] { (Test)\n[Pipeline] { (Test)\n"
        self.assertEqual(errors.stages(log), [{"name": "Test", "status": "passed"}])

    def test_no_pipeline_markers_gives_no_stages(self):
        self.assertEqual(errors.stages("just text\n"), [])


class ExtractTest(unittest.TestCase):
    def test_empty_log(self):
        self.assertEqual(errors.extract(""), {"stage": None, "node": None, "text": ""})

    def test_shell_failure_keeps_its_output_and_the_jenkins_error(self):
        part = errors.extract(PIPELINE)
        self.assertEqual(part["stage"], "Build")
        self.assertIn("make: *** [all] Error 2", part["text"])
        self.assertIn("ERROR: script returned exit code 2", part["text"])
        self.assertNotIn("git fetch", part["text"])

    def test_extract_error_returns_only_the_text(self):
        self.assertEqual(errors.extract_error(PIPELINE), errors.extract(PIPELINE)["text"])

    def test_package_manager_noise_is_dropped(self):
        log = PIPELINE.replace("+ make\n", "+ npm ci\nnpm warn deprecated left-pad@1.0.0\nHit:1 http://deb.example\n")
        text = errors.extract(log)["text"]
        self.assertNotIn("npm warn deprecated", text)
        self.assertNotIn("Hit:1", text)


class TidyTest(unittest.TestCase):
    def test_long_output_keeps_head_and_tail_with_an_omitted_count(self):
        lines = [f"line {i}" for i in range(100)]
        out = errors._tidy(lines, keep_tail=10)  # noqa: SLF001
        self.assertEqual(out[:15], lines[:15])
        self.assertEqual(out[-10:], lines[-10:])
        self.assertEqual(out[15], "... [75 lines omitted] ...")

    def test_runs_of_blank_lines_collapse(self):
        self.assertEqual(errors._tidy(["a", "", "", "", "b"], keep_tail=10), ["a", "", "b"])  # noqa: SLF001


class FingerprintTest(unittest.TestCase):
    def test_numbers_and_hashes_are_blanked_so_builds_match(self):
        a = errors.fingerprint_lines("fatal: build 41 failed for commit deadbeef1234\nok")
        b = errors.fingerprint_lines("fatal: build 97 failed for commit 0123abcd9999\nok")
        self.assertEqual(a, b)
        self.assertEqual(a, ["fatal: build # failed for commit #"])

    def test_only_the_last_limit_lines_are_kept(self):
        text = "\n".join(f"error number {i} happened" for i in range(30))
        self.assertEqual(len(errors.fingerprint_lines(text, limit=5)), 5)

    def test_stage_and_node_banner_lines_are_ignored(self):
        self.assertEqual(errors.fingerprint_lines("Stage: Build failed\nRunning on agent error\nFinished: FAILURE"), [])


if __name__ == "__main__":
    unittest.main()
