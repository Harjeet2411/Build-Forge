"""Settings.from_env: defaults, parsing of each kind of variable, and the job filters."""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from buildforge_agent.config import Settings


def settings(**env: str) -> Settings:
    with mock.patch.dict(os.environ, env, clear=True):
        return Settings.from_env()


class DefaultsTest(unittest.TestCase):
    def test_empty_environment(self):
        s = settings()
        self.assertEqual((s.listen_host, s.listen_port), ("127.0.0.1", 8791))
        self.assertEqual(s.data_dir, Path("/var/lib/buildforge"))
        self.assertEqual(s.send, "errors")
        self.assertEqual(s.notify_on, {"infrastructure"})
        self.assertIsNone(s.job_include)
        self.assertTrue(s.alerts)
        self.assertEqual(s.retention_days, 365)

    def test_blank_values_fall_back_to_defaults(self):
        s = settings(BF_RUNNER_TIMEOUT="  ", BF_SEND=" ", BF_DECK_MODE="", BF_ALERTS="")
        self.assertEqual(s.runner_timeout, 300)
        self.assertEqual(s.send, "errors")
        self.assertEqual(s.deck_mode, "fallback")
        self.assertTrue(s.alerts)


class ParsingTest(unittest.TestCase):
    def test_listen_address(self):
        s = settings(BF_LISTEN="0.0.0.0:9000")
        self.assertEqual((s.listen_host, s.listen_port), ("0.0.0.0", 9000))
        self.assertEqual(settings(BF_LISTEN=":9001").listen_host, "127.0.0.1")

    def test_numbers_and_strings(self):
        s = settings(BF_RUNNER_TIMEOUT="600", BF_DECK_THRESHOLD="0.7", BF_SEND="FULL",
                     BF_RUNNER_URL=" http://10.0.2.15:8796/ ", BF_SHARED_SECRET=" s3cret\n")
        self.assertEqual(s.runner_timeout, 600)
        self.assertEqual(s.deck_threshold, 0.7)
        self.assertEqual(s.send, "full")
        self.assertEqual(s.runner_url, "http://10.0.2.15:8796")
        self.assertEqual(s.shared_secret, "s3cret")

    def test_booleans(self):
        for off in ("0", "false", "No", "OFF"):
            with self.subTest(off=off):
                self.assertFalse(settings(BF_ALERTS=off).alerts)
        for on in ("1", "true", "yes", "anything"):
            with self.subTest(on=on):
                self.assertTrue(settings(BF_ALERTS=on).alerts)

    def test_notify_on_list(self):
        self.assertEqual(settings(BF_NOTIFY_ON=" infrastructure , code ,,").notify_on, {"infrastructure", "code"})


class InvalidValueTest(unittest.TestCase):
    def test_errors_name_the_variable(self):
        cases = {
            "BF_RUNNER_TIMEOUT": ("5m", "BF_RUNNER_TIMEOUT must be a whole number, got '5m'"),
            "BF_DECK_THRESHOLD": ("high", "BF_DECK_THRESHOLD must be a number, got 'high'"),
            "BF_JOB_INCLUDE": ("(payments", "BF_JOB_INCLUDE is not a valid regular expression"),
        }
        for name, (value, message) in cases.items():
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, re.escape(message)):
                settings(**{name: value})


class JobFilterTest(unittest.TestCase):
    def test_no_filters_allow_everything(self):
        self.assertTrue(settings().job_allowed("anything/main"))

    def test_include_and_exclude(self):
        s = settings(BF_JOB_INCLUDE=r"^(payments|checkout)/", BF_JOB_EXCLUDE=r"/sandbox$")
        self.assertTrue(s.job_allowed("payments/main"))
        self.assertFalse(s.job_allowed("search/main"))
        self.assertFalse(s.job_allowed("checkout/sandbox"))


class EnvironmentPatternTest(unittest.TestCase):
    def test_default_patterns(self):
        s = settings()
        self.assertTrue(s.env_prod.search("payments-prod-deploy"))
        self.assertTrue(s.env_prod.search("Deploy PRODUCTION"))
        self.assertFalse(s.env_prod.search("payments-pre-prod"))
        self.assertFalse(s.env_prod.search("product-catalog"))
        self.assertTrue(s.env_uat.search("payments-preprod"))
        self.assertTrue(s.env_uat.search("deploy_uat"))

    def test_overrides(self):
        s = settings(BF_ENV_PROD=r"live", BF_ENV_UAT=r"qa")
        self.assertTrue(s.env_prod.search("live-deploy"))
        self.assertTrue(s.env_uat.search("qa-run"))


if __name__ == "__main__":
    unittest.main()
