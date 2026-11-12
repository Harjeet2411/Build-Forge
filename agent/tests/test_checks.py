"""checks.py: the `check` command's report lines and exit code, with the runner and Teams mocked out."""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildforge_agent import checks  # noqa: E402


def engine(tmp, runner_url="http://runner", teams_url=""):
    settings = SimpleNamespace(runner_url=runner_url, runner_secret="s", runner_timeout=5,
                               teams_webhook_url=teams_url, shared_secret="x", data_dir="/data")
    return SimpleNamespace(s=settings, logs=SimpleNamespace(dir=Path(tmp) / "logs"))


def run(eng, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = checks.run_checks(eng, **kw)
    return code, out.getvalue()


ANALYSIS = {"analysis": {"category": "infrastructure", "rebuild_can_fix": False, "error_observed": "dns"}}


class RunChecksTest(unittest.TestCase):
    def test_everything_reachable_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(checks.kiro, "healthz", return_value={"agent": "1", "kiro": "ok"}), \
                mock.patch.object(checks.kiro, "analyze", return_value=ANALYSIS):
            code, out = run(engine(tmp))
        self.assertEqual(code, 0)
        self.assertIn("OK   Incident log folder", out)
        self.assertIn("OK   Kiro agent: infrastructure", out)
        self.assertIn("SKIP Teams webhook: TEAMS_WEBHOOK_URL not set", out)

    def test_missing_runner_url_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run(engine(tmp, runner_url=""))
        self.assertEqual(code, 1)
        self.assertIn("FAIL Build Forge runner: BF_RUNNER_URL is not set", out)

    def test_unreachable_runner_fails_and_skips_the_kiro_call(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(checks.kiro, "healthz", side_effect=OSError("refused")), \
                mock.patch.object(checks.kiro, "analyze") as analyze:
            code, out = run(engine(tmp))
        self.assertEqual(code, 1)
        self.assertIn("unreachable at http://runner: refused", out)
        analyze.assert_not_called()

    def test_no_kiro_flag_skips_the_analysis(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(checks.kiro, "healthz", return_value={}), \
                mock.patch.object(checks.kiro, "analyze") as analyze:
            code, out = run(engine(tmp), ask_kiro=False)
        self.assertEqual(code, 0)
        self.assertIn("SKIP Kiro agent: skipped (--no-kiro)", out)
        analyze.assert_not_called()

    def test_teams_card_is_posted_only_on_request(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(checks.kiro, "healthz", return_value={}), \
                mock.patch.object(checks.teams, "post") as post:
            eng = engine(tmp, teams_url="https://teams.example/hook")
            _, out = run(eng, ask_kiro=False)
            self.assertIn("add --teams to post a test card", out)
            post.assert_not_called()
            code, out = run(eng, ask_kiro=False, post_teams=True)
        self.assertEqual(code, 0)
        self.assertIn("OK   Teams webhook: test card posted", out)
        post.assert_called_once()

    def test_teams_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(checks.kiro, "healthz", return_value={}), \
                mock.patch.object(checks.teams, "post", side_effect=RuntimeError("HTTP 400")):
            code, out = run(engine(tmp, teams_url="https://t/h"), ask_kiro=False, post_teams=True)
        self.assertEqual(code, 1)
        self.assertIn("FAIL Teams webhook: HTTP 400", out)

    def test_unwritable_log_folder_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "logs"
            blocker.write_text("a file, not a folder")
            with mock.patch.object(checks.kiro, "healthz", return_value={}):
                code, out = run(engine(tmp), ask_kiro=False)
        self.assertEqual(code, 1)
        self.assertIn("FAIL Incident log folder", out)


if __name__ == "__main__":
    unittest.main()
