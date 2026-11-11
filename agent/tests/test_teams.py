"""teams.py: the Adaptive Card content and how a failing webhook is reported."""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from buildforge_agent import teams

INCIDENT = {
    "id": "BF-2026-0001", "job": "payments/deploy", "build": 42, "stage": "Terraform apply",
    "build_url": "https://jenkins.example/job/payments/42/",
    "analysis": {"category": "infrastructure", "severity": "high", "rebuild_can_fix": True,
                 "error_observed": "x" * 2000, "proposed_solution": "y" * 3000},
}


def card(payload):
    return payload["attachments"][0]["content"]


class IncidentCardTest(unittest.TestCase):
    def test_title_facts_and_actions(self):
        c = card(teams.incident_card(INCIDENT, "https://bf.example/#BF-2026-0001"))
        self.assertEqual(c["body"][0]["text"], "🔥 Infrastructure failure: payments/deploy #42")
        facts = {f["title"]: f["value"] for f in c["body"][1]["facts"]}
        self.assertEqual(facts, {"Entry": "BF-2026-0001", "Stage": "Terraform apply", "Severity": "high",
                                 "Rebuild can fix it": "yes"})
        self.assertEqual([a["title"] for a in c["actions"]], ["Open build", "Open in Build Forge"])

    def test_long_text_is_cut(self):
        c = card(teams.incident_card(INCIDENT))
        texts = [b["text"] for b in c["body"] if b["type"] == "TextBlock"]
        self.assertEqual((len(texts[2]), len(texts[4])), (800, 1500))

    def test_unanalysed_incident_has_safe_defaults(self):
        c = card(teams.incident_card({"id": "BF-1", "job": "j", "build": 1, "error": "boom"}))
        self.assertEqual(c["body"][0]["text"], "⚠️ Unknown failure: j #1")
        self.assertEqual(c["body"][2 + 1]["text"], "boom")           # falls back to the raw error
        self.assertNotIn("actions", c)                                 # no links, no empty action bar
        facts = {f["title"]: f["value"] for f in c["body"][1]["facts"]}
        self.assertEqual((facts["Stage"], facts["Rebuild can fix it"]), ("-", "no"))

    def test_category_with_underscore(self):
        inc = {**INCIDENT, "analysis": {"category": "test_failure"}}
        self.assertEqual(card(teams.incident_card(inc))["body"][0]["text"], "⚠️ Test failure: payments/deploy #42")


class PostTest(unittest.TestCase):
    def test_needs_a_url(self):
        with self.assertRaisesRegex(RuntimeError, "TEAMS_WEBHOOK_URL is not set"):
            teams.post("", {})

    def test_sends_json(self):
        resp = mock.MagicMock(status=202)
        resp.__enter__.return_value = resp
        with mock.patch("urllib.request.urlopen", return_value=resp) as urlopen:
            teams.post("https://hook.example/x", {"a": 1}, timeout=5)
        req = urlopen.call_args.args[0]
        self.assertEqual((req.full_url, req.get_method(), json.loads(req.data)), ("https://hook.example/x", "POST", {"a": 1}))
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 5)

    def test_http_error_names_the_status_and_body(self):
        err = urllib.error.HTTPError("https://hook.example/x", 429, "Too Many", {}, io.BytesIO(b"slow down"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, r"HTTP 429: b'slow down'"):
                teams.post("https://hook.example/x", {})


if __name__ == "__main__":
    unittest.main()
