"""The notification bell: pipelines that keep failing, by environment, and who to contact."""
from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

import test_service as ts
from buildforge_agent import server
from buildforge_agent.alerts import environment


def commit(author, email="", msg="change", cid="abc1234567"):
    return {"id": cid, "author": author, "author_id": author.lower().replace(" ", "."), "email": email, "message": msg}


class AlertBase(ts.Base):
    def fail(self, job, build, **extra):
        return self.fail_build(job=job, build=build, **extra)

    def ok(self, job, build):
        self.e.build_result(job, build, "SUCCESS", None, time.time())
        self.e.evaluate_alerts(job)

    def streak(self, job, n, start=1, **extra):
        for b in range(start, start + n):
            self.fail(job, b, **extra)

    def open_alerts(self, user="admin"):
        return self.e.alerts.list(user)


class EnvironmentTests(AlertBase):
    def test_environment_comes_from_the_pipeline_name(self):
        for job, env in (("deploy/prod-api", "prod"), ("prod/web/main", "prod"), ("web-production", "prod"),
                         ("web/prd", "prod"), ("deploy-uat", "uat"), ("shop/staging/deploy", "uat"),
                         ("deploy/pre-prod", "uat"), ("deploy/preprod", "uat"), ("deploy/dev", "other"),
                         ("product-catalog", "other"), ("production-ready-tests", "prod"), ("improd", "other")):
            self.assertEqual(environment(self.s, job), env, job)


class ThresholdTests(AlertBase):
    def test_production_fires_critical_after_more_than_three_failures(self):
        self.streak("deploy/prod-api", 3)
        self.assertEqual(self.open_alerts()["items"], [])
        self.fail("deploy/prod-api", 4)
        a = self.open_alerts()["items"]
        self.assertEqual([(x["severity"], x["env"], x["streak"]) for x in a], [("critical", "prod", 4)])
        self.assertEqual((a[0]["first_build"], a[0]["last_build"]), (1, 4))
        self.fail("deploy/prod-api", 5)
        a = self.open_alerts()["items"]
        self.assertEqual((len(a), a[0]["streak"]), (1, 5))  # the same alert grows, no second one

    def test_uat_is_high_after_more_than_five_and_other_pipelines_low(self):
        self.streak("deploy/uat-api", 5)
        self.streak("tools/cleanup", 5)
        self.assertEqual(self.open_alerts()["items"], [])
        self.fail("deploy/uat-api", 6)
        self.fail("tools/cleanup", 6)
        got = {x["job"]: x["severity"] for x in self.open_alerts()["items"]}
        self.assertEqual(got, {"deploy/uat-api": "high", "tools/cleanup": "low"})

    def test_sorted_critical_first_with_counts(self):
        self.streak("tools/cleanup", 6)
        self.streak("deploy/uat-api", 6)
        self.streak("deploy/prod-api", 4)
        out = self.open_alerts()
        self.assertEqual([x["severity"] for x in out["items"]], ["critical", "high", "low"])
        self.assertEqual((out["counts"]["open"], out["counts"]["unread"], out["counts"]["critical"]), (3, 3, 1))

    def test_a_green_build_discards_the_alert_and_the_next_streak_starts_over(self):
        self.streak("deploy/prod-api", 4)
        self.ok("deploy/prod-api", 5)
        self.assertEqual(self.open_alerts()["items"], [])
        resolved = self.e.alerts.list("admin", "resolved")["items"]
        self.assertEqual((len(resolved), resolved[0]["resolved_build"]), (1, 5))
        self.streak("deploy/prod-api", 3, start=6)
        self.assertEqual(self.open_alerts()["items"], [])  # three again: not more than three
        self.fail("deploy/prod-api", 9)
        a = self.open_alerts()["items"]
        self.assertEqual((len(a), a[0]["first_build"]), (1, 6))

    def test_aborted_builds_neither_extend_nor_end_a_streak(self):
        self.streak("deploy/prod-api", 3)
        self.e.build_result("deploy/prod-api", 4, "ABORTED", None, time.time())
        self.e.evaluate_alerts("deploy/prod-api")
        self.assertEqual(self.open_alerts()["items"], [])
        self.fail("deploy/prod-api", 5)
        self.assertEqual(self.open_alerts()["items"][0]["streak"], 4)

    def test_settings_change_the_limits_and_alerts_can_be_turned_off(self):
        self.s.alert_prod_after = 1
        self.streak("deploy/prod-api", 2)
        self.assertEqual(len(self.open_alerts()["items"]), 1)
        self.s.alerts = False
        self.streak("deploy/prod-web", 6)
        self.assertEqual(len(self.open_alerts()["items"]), 1)

    def test_alerts_are_rebuilt_for_failures_from_before_the_upgrade(self):
        self.s.alerts = False
        self.streak("deploy/prod-api", 5)
        self.s.alerts = True
        self.assertEqual(self.e.alerts.rebuild_all(), 1)
        self.assertEqual(self.open_alerts()["items"][0]["streak"], 5)


class ReadAndDismissTests(AlertBase):
    def setUp(self):
        super().setUp()
        self.streak("deploy/prod-api", 4)
        self.alert = self.open_alerts()["items"][0]

    def test_read_is_per_person(self):
        self.e.alerts.mark_read("alice", [self.alert["id"]])
        self.assertTrue(self.open_alerts("alice")["items"][0]["read"])
        self.assertEqual(self.open_alerts("alice")["counts"]["unread"], 0)
        self.assertFalse(self.open_alerts("bob")["items"][0]["read"])
        self.assertEqual(self.open_alerts("bob")["counts"]["unread"], 1)
        self.e.alerts.mark_read("alice", [self.alert["id"]], unread=True)
        self.assertEqual(self.open_alerts("alice")["counts"]["unread"], 1)
        self.e.alerts.mark_read("alice", everything=True)
        self.assertEqual(self.open_alerts("alice")["counts"]["unread"], 0)
        with self.assertRaises(ValueError):
            self.e.alerts.mark_read("", [self.alert["id"]])

    def test_dismiss_is_for_everyone_and_stays_dismissed_until_the_streak_ends(self):
        self.e.alerts.dismiss(self.alert["id"], "alice")
        self.assertEqual(self.open_alerts("bob")["items"], [])
        gone = self.e.alerts.list("bob", "dismissed")["items"]
        self.assertEqual((gone[0]["dismissed_by"], gone[0]["status"]), ("alice", "dismissed"))
        self.fail("deploy/prod-api", 5)  # still failing: it must not pop up again
        self.assertEqual(self.open_alerts()["items"], [])
        self.assertEqual(self.e.alerts.list("bob", "dismissed")["items"][0]["streak"], 5)
        self.e.alerts.restore(self.alert["id"], "alice")
        self.assertEqual(len(self.open_alerts()["items"]), 1)
        self.e.alerts.dismiss(self.alert["id"], "alice")
        self.ok("deploy/prod-api", 6)
        self.assertEqual(self.e.alerts.list("bob", "dismissed")["items"], [])
        self.assertEqual(len(self.e.alerts.list("bob", "resolved")["items"]), 1)


class ContactTests(AlertBase):
    def test_who_started_it_and_who_committed(self):
        trig = {"kind": "user", "id": "ravi", "name": "Ravi Kumar"}
        self.fail("deploy/prod-api", 1, triggered_by=trig, commits=[commit("Asha Rao", "asha@example.com", "Fix the retry loop\nlong body")])
        self.fail("deploy/prod-api", 2, triggered_by={"kind": "scm", "name": "SCM change"},
                  commits=[commit("Ben Lee", "ben@example.com", "Bump deps"), commit("Asha Rao", "asha@example.com")])
        self.fail("deploy/prod-api", 3, triggered_by={"kind": "timer", "name": "Timer"})
        self.fail("deploy/prod-api", 4, triggered_by=trig)
        c = self.open_alerts()["items"][0]["contacts"]
        self.assertEqual(c["started_first"]["name"], "Ravi Kumar")
        self.assertEqual((c["started_latest"]["id"], c["started_latest"]["build"]), ("ravi", 4))
        self.assertEqual([p["name"] for p in c["committers"]], ["Asha Rao", "Ben Lee"])
        asha, ben = c["committers"]
        self.assertEqual((asha["email"], asha["builds"], asha["first_failure"]), ("asha@example.com", [1, 2], True))
        self.assertEqual((ben["builds"], ben["first_failure"]), ([2], False))
        self.assertEqual(asha["commits"][0]["message"], "Fix the retry loop")  # first line only

    def test_commits_and_triggers_are_cleaned_and_stored_on_the_incident(self):
        inc = self.fail("tools/x", 1, triggered_by={"kind": "user", "id": "x" * 500, "name": "N", "junk": 1},
                        commits=[{"author": "A" * 300, "message": "m"}, "bad", {"message": "no author"}] + [commit("P")] * 40)
        self.assertEqual(len(inc["triggered_by"]["id"]), 120)
        self.assertNotIn("junk", inc["triggered_by"])
        self.assertEqual(len(inc["commits"]), 20)
        self.assertEqual(len(inc["commits"][0]["author"]), 120)
        self.assertIsNone(self.fail("tools/x", 2)["commits"])


class AlertHttpTests(AlertBase):
    def setUp(self):
        super().setUp()
        self.streak("deploy/prod-api", 4, commits=[commit("Asha Rao", "asha@example.com")])
        server.Handler.engine = self.e
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        super().tearDown()

    def call(self, path, body=None):
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                     method="GET" if body is None else "POST",
                                     headers={"X-BuildForge-Secret": "s3", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_list_read_dismiss_restore_over_http(self):
        code, out = self.call("/api/v2/alerts?user=alice")
        self.assertEqual((code, out["counts"]["unread"], out["items"][0]["severity"]), (200, 1, "critical"))
        aid = out["items"][0]["id"]
        self.assertEqual(out["items"][0]["contacts"]["committers"][0]["name"], "Asha Rao")
        self.assertEqual(self.call("/api/v2/alerts/read", {"user": "alice", "ids": [aid]})[1]["changed"], 1)
        self.assertEqual(self.call("/api/v2/alerts?user=alice")[1]["counts"]["unread"], 0)
        self.assertEqual(self.call("/api/v2/alerts/read", {"ids": [aid]})[0], 400)  # no user
        self.assertEqual(self.call("/api/v2/alerts/dismiss", {"id": aid, "user": "alice"})[1]["status"], "dismissed")
        self.assertEqual(self.call("/api/v2/alerts?user=alice")[1]["items"], [])
        self.assertEqual(self.call("/api/v2/alerts?status=dismissed")[1]["counts"]["dismissed"], 1)
        self.assertEqual(self.call("/api/v2/alerts/restore", {"id": aid, "user": "alice"})[1]["status"], "open")
        self.assertEqual(self.call("/api/v2/alerts/dismiss", {"id": "AL-NOPE", "user": "a"})[0], 404)

    def test_a_green_build_over_http_resolves_it(self):
        self.call("/api/v2/build-result", {"job": "deploy/prod-api", "build": 5, "result": "SUCCESS",
                                           "timestamp": time.time() * 1000, "duration_ms": 10})
        self.assertEqual(self.call("/api/v2/alerts")[1]["items"], [])

    def test_the_secret_is_required(self):
        req = urllib.request.Request(self.url + "/api/v2/alerts", headers={"X-BuildForge-Secret": "no"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        cm.exception.close()
        self.assertEqual(cm.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
