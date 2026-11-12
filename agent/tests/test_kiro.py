"""kiro.py: how the runner client turns HTTP and network failures into readable RunnerError messages."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildforge_agent import kiro  # noqa: E402


class Runner(BaseHTTPRequestHandler):
    status, payload, seen = 200, b"{}", {}

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        Runner.seen = {"secret": self.headers.get("X-BuildForge-Secret"),
                       "body": json.loads(self.rfile.read(int(self.headers["Content-Length"])))}
        self.send_response(Runner.status)
        self.end_headers()
        self.wfile.write(Runner.payload)


class AnalyzeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Runner)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def answer(self, status, payload):
        Runner.status = status
        Runner.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def test_success_sends_secret_and_returns_body(self):
        self.answer(200, {"analysis": {"category": "test_failure"}, "seconds": 3})
        out = kiro.analyze(self.url, "s3cret", {"job": "a"}, 5)
        self.assertEqual(out["analysis"]["category"], "test_failure")
        self.assertEqual(Runner.seen, {"secret": "s3cret", "body": {"job": "a"}})

    def test_empty_url(self):
        with self.assertRaisesRegex(kiro.RunnerError, "BF_RUNNER_URL is not set"):
            kiro.analyze("", "s", {}, 5)

    def test_http_error_shows_message_and_stderr_tail(self):
        self.answer(500, {"error": "kiro crashed", "stderr_tail": "  boom  "})
        with self.assertRaisesRegex(kiro.RunnerError, r"HTTP 500: kiro crashed \(boom\)"):
            kiro.analyze(self.url, "s", {}, 5)

    def test_http_error_with_unreadable_body_falls_back_to_status(self):
        self.answer(502, b"<html>bad gateway</html>")
        with self.assertRaisesRegex(kiro.RunnerError, "HTTP 502: HTTP 502"):
            kiro.analyze(self.url, "s", {}, 5)

    def test_non_json_success_body(self):
        self.answer(200, b"hello")
        with self.assertRaisesRegex(kiro.RunnerError, "not JSON"):
            kiro.analyze(self.url, "s", {}, 5)

    def test_missing_analysis(self):
        self.answer(200, {"seconds": 1})
        with self.assertRaisesRegex(kiro.RunnerError, "without an analysis"):
            kiro.analyze(self.url, "s", {}, 5)

    def test_unreachable_runner(self):
        with self.assertRaisesRegex(kiro.RunnerError, "unreachable at http://127.0.0.1:9"):
            kiro.analyze("http://127.0.0.1:9", "s", {}, 2)


if __name__ == "__main__":
    unittest.main()
