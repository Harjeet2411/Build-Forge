"""HTTP API used by the Build Forge Jenkins plugin. Every call carries X-BuildForge-Secret; browsers never call it
directly (the plugin proxies them, with Jenkins permissions)."""
from __future__ import annotations

import hmac
import json
import logging
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import Engine

log = logging.getLogger("buildforge.http")
MAX_BODY = 20 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    engine: Engine  # set by serve()
    server_version = "BuildForge/2"

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request body too large")
        body = json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8") or "{}")
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        return body

    def _authorised(self) -> bool:
        secret = self.engine.s.shared_secret
        given = self.headers.get("X-BuildForge-Secret", "")
        return bool(secret) and hmac.compare_digest(given.encode(), secret.encode())

    def do_GET(self):  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        e = self.engine
        if url.path == "/healthz":
            return self._send(200, {"ok": True, "version": "2.0.0", "runner": bool(e.s.runner_url),
                                    "teams": bool(e.s.teams_webhook_url), "log_dir": str(e.logs.dir)})
        if not self._authorised():
            return self._send(401, {"error": "missing or wrong X-BuildForge-Secret"})
        try:
            if url.path == "/api/v2/analysis":
                view = e.view(q["job"], int(q["build"]))
                return self._send(200 if view else 404, view or {"status": "missing", "error": "not analysed"})
            if url.path == "/api/v2/incidents":
                days = int(q.get("days", 30))
                total, rows = e.store.list_incidents(
                    limit=min(int(q.get("limit", 50)), 200), offset=int(q.get("offset", 0)), q=q.get("q", ""),
                    category=q.get("category", ""), outcome=q.get("outcome", ""),
                    since=time.time() - days * 86400 if days > 0 else 0)
                return self._send(200, {"total": total, "items": rows})
            if url.path == "/api/v2/incident":
                d = e.detail(q["id"])
                return self._send(200 if d else 404, d or {"error": "no such entry"})
            if url.path == "/api/v2/stats":
                return self._send(200, e.stats(int(q.get("days", 30))))
        except (KeyError, ValueError):
            return self._send(400, {"error": "missing or bad query parameters"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        e = self.engine
        if not self._authorised():
            return self._send(401, {"error": "missing or wrong X-BuildForge-Secret"})
        try:
            body = self._body()
            if path == "/api/v2/failure":
                if not body.get("job") or not str(body.get("build", "")).isdigit():
                    raise ValueError("job and build are required")
                return self._send(202, e.submit(body))
            if path == "/api/v2/build-result":
                ts = (float(body.get("timestamp") or 0) + float(body.get("duration_ms") or 0)) / 1000 or time.time()
                rebuild_of = int(body["rebuild_of"]) if str(body.get("rebuild_of") or "").isdigit() else None
                e.build_result(str(body["job"]), int(body["build"]), str(body.get("result") or "SUCCESS"),
                               rebuild_of, ts)
                return self._send(200, {"ok": True})
            if path == "/api/v2/rebuild-event":
                e.rebuild_event(body)
                return self._send(200, {"ok": True})
            if path == "/api/v2/reanalyze":
                out = e.reanalyze(str(body["id"]))
                return self._send(404 if "error" in out else 202, out)
        except (KeyError, ValueError) as exc:
            return self._send(400, {"error": str(exc) or "bad request"})
        return self._send(404, {"error": "not found"})


def _pruner(engine: Engine) -> None:
    while True:
        try:
            n = engine.prune()
            if n:
                log.info("pruned %d incidents older than %d days", n, engine.s.retention_days)
        except Exception:  # noqa: BLE001
            log.exception("pruning failed")
        time.sleep(86400)


def serve(engine: Engine) -> None:
    Handler.engine = engine
    httpd = ThreadingHTTPServer((engine.s.listen_host, engine.s.listen_port), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=_pruner, args=(engine,), daemon=True).start()
    log.info("Build Forge service on %s:%s, runner %s, incident logs in %s", engine.s.listen_host, engine.s.listen_port,
             engine.s.runner_url or "(not set)", engine.logs.dir)
    httpd.serve_forever()
