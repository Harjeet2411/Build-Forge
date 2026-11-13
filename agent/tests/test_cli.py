"""python -m buildforge_agent: exit codes and messages of the show, extract and start-up checks."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildforge_agent import __main__ as cli  # noqa: E402


def run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("BF_", "TEAMS_"))}
    with mock.patch.dict(os.environ, {**clean, **env}, clear=True), mock.patch.object(sys, "argv", ["buildforge_agent", *argv]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main()
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {"BF_LOG_DIR": self.tmp.name, "BF_DATA_DIR": self.tmp.name}

    def test_show_prints_an_existing_incident_log(self):
        folder = Path(self.tmp.name) / "incidents"
        folder.mkdir()
        (folder / "BF-20261113-ABC234.md").write_text("# incident\n", encoding="utf-8")
        code, out, _ = run(["show", "BF-20261113-ABC234"], self.env)
        self.assertEqual((code, out), (0, "# incident\n\n"))

    def test_show_reports_a_missing_incident_with_exit_1(self):
        code, out, _ = run(["show", "BF-20261113-NOPE22"], self.env)
        self.assertEqual(code, 1)
        self.assertIn("no incident log BF-20261113-NOPE22", out)

    def test_extract_reports_an_unreadable_file_with_exit_2(self):
        code, _, err = run(["extract", str(Path(self.tmp.name) / "missing.log")], self.env)
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)

    def test_extract_prints_the_failing_stage(self):
        log = Path(self.tmp.name) / "console.log"
        log.write_text("[Pipeline] { (Build)\n+ make\nmake: *** [all] Error 2\nERROR: script returned exit code 2\n",
                       encoding="utf-8")
        code, out, _ = run(["extract", str(log)], self.env)
        self.assertEqual(code, 0)
        self.assertIn('"stage"', out)
        self.assertIn("make: *** [all] Error 2", out)

    def test_bad_configuration_exits_2_and_names_the_variable(self):
        code, _, err = run(["check"], {**self.env, "BF_DECK_THRESHOLD": "7"})
        self.assertEqual(code, 2)
        self.assertIn("BF_DECK_THRESHOLD", err)

    def test_missing_shared_secret_exits_2(self):
        code, _, err = run(["check"], self.env)
        self.assertEqual(code, 2)
        self.assertIn("BF_SHARED_SECRET is not set", err)


if __name__ == "__main__":
    unittest.main()
