"""Build Forge service tests: python3 -m unittest discover -s tests (from the agent folder). No network needed."""
from __future__ import annotations

import json
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from buildforge_agent import errors, incidents, server  # noqa: E402
from buildforge_agent.config import Settings  # noqa: E402
from buildforge_agent.engine import Engine, admin_url  # noqa: E402
from buildforge_agent.store import Store  # noqa: E402

LOGS = HERE / "logs"


class StubRunner(BaseHTTPRequestHandler):
    """Stands in for the Build Forge runner: answers like the Kiro agent would through submit_analysis."""
    seen: list = []
    teams: list = []
    fail = False

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/teams":
            StubRunner.teams.append(body)
            self.send_response(202)
            self.end_headers()
            return
        StubRunner.seen.append((self.headers.get("X-BuildForge-Secret"), body))
        if StubRunner.fail:
            out, code = {"error": "the Kiro agent did not submit an analysis (kiro-cli exit 1)",
                         "stderr_tail": "not logged in"}, 502
        else:
            transient = "Could not resolve host" in body["error"]
            out, code = {"seconds": 12.5, "agent": "buildforge-fixer", "analysis": {
                "error_observed": "`git fetch` could not resolve the host." if transient else
                                  "`aws elbv2 create-target-group` failed: `DuplicateTargetGroupName`.",
                "proposed_solution": "1. Rebuild.\n2. Check DNS." if transient else "1. Reuse the target group.",
                "category": "transient" if transient else "configuration", "severity": "medium",
                "confidence": 0.8, "rebuild_can_fix": transient and not body["history"].get("rebuilt_and_failed_again"),
                "rebuild_reason": "DNS hiccup." if transient else "The name already exists.",
                "signature": "git-dns" if transient else "aws-duplicate-target-group"}}, 200
        data = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


DNS_LOG = """Started by user admin
[Pipeline] node
Running on built-in in /var/jenkins_home/workspace/app
[Pipeline] {
[Pipeline] stage
[Pipeline] { (Checkout)
[Pipeline] sh
+ git fetch origin
fatal: unable to access 'https://git.example.com/app.git/': Could not resolve host: git.example.com
[Pipeline] }
[Pipeline] // stage
[Pipeline] stage
[Pipeline] { (Deploy)
Stage "Deploy" skipped due to earlier failure(s)
[Pipeline] }
[Pipeline] End of Pipeline
ERROR: script returned exit code 128
Finished: FAILURE
"""


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubRunner)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.runner = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        StubRunner.seen, StubRunner.teams, StubRunner.fail = [], [], False
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.s = Settings(shared_secret="s3", data_dir=root / "data", log_dir=root / "log", runner_url=self.runner,
                          runner_secret="rs", runner_timeout=10, teams_webhook_url=self.runner + "/teams")
        self.e = Engine(self.s, Store(self.s.data_dir / "buildforge.db"))

    def tearDown(self):
        self.e._pool.shutdown(wait=True)  # noqa: SLF001
        self.tmp.cleanup()

    def fail_build(self, job="team/app", build=7, log=DNS_LOG, **extra) -> dict:
        body = {"job": job, "build": build, "result": "FAILURE", "log": log, "params": ["ENV", "TAG"],
                "build_url": f"https://jenkins.example.com/job/{job.replace('/', '/job/')}/{build}/",
                "timestamp": time.time() * 1000, "duration_ms": 1000, **extra}
        out = self.e.submit(body)
        self.assertTrue(self.e.wait_idle(15))  # the analysis, the incident log, the alert and the deck are done
        return self.e.store.incident(out["id"])


class AnalysisTests(Base):
    def test_failure_becomes_an_incident_with_a_kiro_analysis_and_a_log_file(self):
        inc = self.fail_build()
        self.assertRegex(inc["id"], r"^BF-\d{8}-[A-Z2-9]{6}$")
        self.assertEqual(inc["status"], "done")
        self.assertEqual(inc["stage"], "Checkout")
        self.assertEqual(inc["stages"], [{"name": "Checkout", "status": "failed"}, {"name": "Deploy", "status": "skipped"}])
        self.assertTrue(inc["rebuild_can_fix"])
        secret, req = StubRunner.seen[0]
        self.assertEqual(secret, "rs")
        self.assertEqual(req["params"], ["ENV", "TAG"])
        self.assertIn("Could not resolve host", req["error"])
        self.assertNotIn("Started by user", req["error"])  # only the failing part goes to Kiro
        md = (self.s.log_dir / "incidents" / f"{inc['id']}.md").read_text()
        for needle in ("## Error observed", "## Proposed solution", "## Stats", "| Build URL | https://jenkins.example.com/job/team/job/app/7/ |",
                       "Rebuild with the same parameters can fix it: yes", "Waiting for the next build"):
            self.assertIn(needle, md)
        line = json.loads((self.s.log_dir / "incidents.jsonl").read_text().splitlines()[-1])
        self.assertEqual((line["event"], line["id"], line["signature"]), ("analysed", inc["id"], "git-dns"))

    def test_duplicate_report_returns_the_same_entry(self):
        inc = self.fail_build()
        again = self.e.submit({"job": "team/app", "build": 7, "log": DNS_LOG})
        self.assertEqual(again["id"], inc["id"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(StubRunner.seen), 1)

    def test_runner_failure_is_recorded_and_can_be_reanalysed(self):
        StubRunner.fail = True
        inc = self.fail_build()
        self.assertEqual(inc["status"], "error")
        self.assertIn("not logged in", inc["error"])
        self.assertIn("did not submit", (self.s.log_dir / "incidents" / f"{inc['id']}.md").read_text())
        StubRunner.fail = False
        self.e.reanalyze(inc["id"])
        deadline = time.time() + 10
        while time.time() < deadline and self.e.store.incident(inc["id"])["status"] != "done":
            time.sleep(0.05)
        self.assertEqual(self.e.store.incident(inc["id"])["status"], "done")

    def test_same_error_again_carries_history_and_rebuilt_failure_turns_rebuild_off(self):
        first = self.fail_build(build=7)
        self.e.rebuild_event({"job": "team/app", "build": 7, "user": "alice"})
        second = self.fail_build(build=8, rebuild_of=7)
        hist = StubRunner.seen[-1][1]["history"]
        self.assertEqual((hist["same_error_in_job"], hist["last_signature"]), (1, "git-dns"))
        self.assertEqual(hist["rebuilt_and_failed_again"], 1)  # #8 is the rebuild of #7 and failed the same way
        self.assertFalse(second["rebuild_can_fix"])
        third = self.fail_build(build=9)
        self.assertEqual(StubRunner.seen[-1][1]["history"]["rebuilt_and_failed_again"], 1)
        self.assertFalse(third["rebuild_can_fix"])
        self.assertEqual(self.e.store.recurrence(third), {"in_job": 2, "overall": 2})
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_teams_alert_only_for_configured_categories(self):
        self.s.notify_on = {"configuration"}
        self.fail_build(build=1)  # transient: no alert
        self.assertEqual(StubRunner.teams, [])
        log = (LOGS / "aws-duplicate-target-group.log").read_text()
        inc = self.fail_build(build=2, log=log)
        self.assertEqual(len(StubRunner.teams), 1)
        card = StubRunner.teams[0]["attachments"][0]["content"]
        self.assertIn(inc["id"], json.dumps(card))
        self.assertEqual(card["actions"][1]["url"], f"https://jenkins.example.com/buildforge/admin#{inc['id']}")

    def test_excluded_job_is_recorded_but_not_analysed(self):
        self.s.job_exclude = re.compile("^sandbox/")
        out = self.e.submit({"job": "sandbox/x", "build": 1, "log": DNS_LOG, "result": "FAILURE"})
        self.assertEqual(out["status"], "skipped")
        inc = self.e.store.incident(out["id"])
        self.assertEqual((inc["status"], inc["stage"]), ("skipped", "Checkout"))
        self.assertIn("excluded from analysis", inc["error"])
        self.assertEqual(StubRunner.seen, [])  # Kiro never saw it
        self.assertEqual(self.e.stats({})["failed_builds"], 1)  # ...but the dashboard counts it
        self.assertEqual(self.e.stats({})["excluded"], 1)

    def test_plugin_can_exclude_a_failure_from_analysis(self):
        out = self.e.submit({"job": "a", "build": 1, "log": DNS_LOG, "analyse": False})
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(StubRunner.seen, [])

    def test_an_excluded_failure_can_still_be_analysed_on_request(self):
        out = self.e.submit({"job": "a", "build": 1, "log": DNS_LOG, "analyse": False})
        self.e.reanalyze(out["id"])
        deadline = time.time() + 10
        while time.time() < deadline and self.e.store.incident(out["id"])["status"] != "done":
            time.sleep(0.05)
        self.assertEqual(self.e.store.incident(out["id"])["status"], "done")


class OutcomeTests(Base):
    def test_fixed_on_the_next_build_by_a_buildforge_rebuild(self):
        inc = self.fail_build(build=7)
        self.e.rebuild_event({"job": "team/app", "build": 7, "user": "alice"})
        self.e.rebuild_event({"job": "team/app", "build": 7, "phase": "started", "new_build": 8})
        self.e.build_result("team/app", 8, "SUCCESS", 7, time.time() + 60)
        inc = self.e.store.incident(inc["id"])
        self.assertEqual((inc["next_build"], inc["next_result"], inc["next_is_rebuild"], inc["fixed_build"]),
                         (8, "SUCCESS", 1, 8))
        rb = self.e.store.rebuilds_of("team/app", 7)
        self.assertEqual((rb[0]["requested_by"], rb[0]["new_build"], rb[0]["new_result"]), ("alice", 8, "SUCCESS"))
        md = (self.s.log_dir / "incidents" / f"{inc['id']}.md").read_text()
        self.assertIn("Fixed on the next build (#8, a Build Forge rebuild)", md)
        self.assertIn("by alice: #8 -> SUCCESS", md)
        st = self.e.stats({})
        self.assertEqual((st["fixed_next_build"], st["pipelines_fixed_next_build"], st["rebuilds_succeeded"]), (1, 1, 1))

    def test_fixed_later_and_out_of_order_builds(self):
        inc = self.fail_build(build=7)
        self.e.build_result("team/app", 10, "SUCCESS", None, time.time())  # finishes first
        self.e.build_result("team/app", 8, "FAILURE", None, time.time())
        inc = self.e.store.incident(inc["id"])
        self.assertEqual((inc["next_build"], inc["next_result"], inc["fixed_build"]), (8, "FAILURE", 10))
        st = self.e.stats({})
        self.assertEqual((st["fixed_next_build"], st["fixed_later"], st["still_failing"]), (0, 1, 0))

    def test_stats_and_listing(self):
        self.fail_build(job="a", build=1)
        self.fail_build(job="b", build=3, log=(LOGS / "aws-duplicate-target-group.log").read_text())
        self.e.build_result("a", 2, "SUCCESS", None, time.time())
        st = self.e.stats({})
        self.assertEqual((st["failed_builds"], st["failed_pipelines"], st["analysed"], st["still_failing"]), (2, 2, 2, 1))
        self.assertEqual(st["by_category"], {"transient": 1, "configuration": 1})
        self.assertEqual(st["rebuild_suggested"], 1)
        total, rows = self.e.store.list_incidents({"outcome": "open"})
        self.assertEqual((total, rows[0]["job"]), (1, "b"))
        total, rows = self.e.store.list_incidents({"q": "duplicate"})
        self.assertEqual(total, 1)

    def test_prune_removes_old_incidents_and_their_files(self):
        inc = self.fail_build()
        self.e.store._exec("UPDATE incidents SET created=0")  # noqa: SLF001
        self.assertEqual(self.e.prune(), 1)
        self.assertFalse((self.s.log_dir / "incidents" / f"{inc['id']}.md").exists())


class HttpTests(Base):
    def setUp(self):
        super().setUp()
        server.Handler.engine = self.e
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        super().tearDown()

    def call(self, path, body=None, secret="s3"):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-BuildForge-Secret": secret, "Content-Type": "application/json"},
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_secret_is_required(self):
        self.assertEqual(self.call("/api/v2/stats", secret="nope")[0], 401)
        self.assertEqual(self.call("/healthz", secret="")[0], 200)

    def test_plugin_round_trip(self):
        code, out = self.call("/api/v2/failure", {"job": "app", "build": 4, "log": DNS_LOG, "build_url": "https://j/job/app/4/"})
        self.assertEqual(code, 202)
        for _ in range(200):
            code, view = self.call("/api/v2/analysis?job=app&build=4")
            if view.get("status") == "done":
                break
            time.sleep(0.05)
        self.assertEqual((view["id"], view["stage"], view["analysis"]["rebuild_can_fix"]), (out["id"], "Checkout", True))
        self.assertEqual(self.call("/api/v2/rebuild-event", {"job": "app", "build": 4, "user": "u1"})[0], 200)
        self.assertEqual(self.call("/api/v2/build-result", {"job": "app", "build": 5, "result": "SUCCESS", "rebuild_of": 4})[0], 200)
        code, detail = self.call(f"/api/v2/incident?id={out['id']}")
        self.assertEqual((detail["next_result"], detail["rebuilds"][0]["new_build"]), ("SUCCESS", 5))
        self.assertIn("## Proposed solution", detail["log_text"])
        code, lst = self.call("/api/v2/incidents?outcome=fixed-next")
        self.assertEqual(lst["total"], 1)
        self.assertEqual(self.call("/api/v2/analysis?job=app&build=99")[0], 404)
        self.assertEqual(self.call("/api/v2/failure", {"job": "app"})[0], 400)


class SmallTests(unittest.TestCase):
    def test_extract_samples(self):
        part = errors.extract((LOGS / "npm-build-crash.log").read_text())
        self.assertEqual(part["stage"], "install dependecies and build")
        self.assertLess(len(part["text"]), 5000)

    def test_fingerprint_ignores_numbers(self):
        a = incidents.fingerprint("Error: timeout after 30s on 10.0.0.1\nERROR: script returned exit code 1")
        b = incidents.fingerprint("Error: timeout after 45s on 10.0.0.9\nERROR: script returned exit code 2")
        self.assertEqual(a, b)
        self.assertIsNone(incidents.fingerprint("all good"))

    def test_admin_url(self):
        self.assertEqual(admin_url("https://j.example.com/job/a/job/b/3/", "BF-1"), "https://j.example.com/buildforge/admin#BF-1")
        self.assertEqual(admin_url("https://j.example.com/ci/job/a/3/", "BF-1"), "https://j.example.com/ci/buildforge/admin#BF-1")
        self.assertEqual(admin_url(None, "BF-1"), "")


if __name__ == "__main__":
    unittest.main()
