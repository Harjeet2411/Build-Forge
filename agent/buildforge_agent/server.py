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

from . import export
from .engine import Engine

log = logging.getLogger("buildforge.http")
MAX_BODY = 20 * 1024 * 1024


def _num(raw, default=0.0) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def parse_filters(q: dict) -> dict:
    """The dashboard filter from a query string (shared by the list, the numbers and the exports).
    from / to are epoch seconds ("to" is exclusive); without them `days` looks back from now (0 = all time)."""
    f: dict = {}
    if q.get("from"):
        f["since"] = _num(q["from"])
    elif q.get("days") not in (None, ""):
        days = int(_num(q["days"], 30))
        if days > 0:
            f["since"] = time.time() - days * 86400
    if q.get("to"):
        f["until"] = _num(q["to"])
    f["q"] = q.get("q", "")
    f["jobs"] = [x for x in q.get("jobs", "").split("|") if x.strip()]
    f["categories"] = [x for x in q.get("category", "").split(",") if x.strip()]
    for key in ("outcome", "analysis", "rebuild", "result"):
        f[key] = q.get(key, "")
    return f


def tz_seconds(q: dict) -> int:
    """The viewer's UTC offset (minutes east, as the browser reports it) -> seconds, so days and times are theirs."""
    return int(max(-14 * 60, min(14 * 60, _num(q.get("tz"), 0))) * 60)


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

    def _send_file(self, data: bytes, ctype: str, filename: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
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
            return self._send(200, {"ok": True, "version": "2.1.0", "runner": bool(e.s.runner_url),
                                    "teams": bool(e.s.teams_webhook_url), "log_dir": str(e.logs.dir)})
        if not self._authorised():
            return self._send(401, {"error": "missing or wrong X-BuildForge-Secret"})
        try:
            if url.path == "/api/v2/analysis":
                view = e.view(q["job"], int(q["build"]))
                return self._send(200 if view else 404, view or {"status": "missing", "error": "not analysed"})
            if url.path == "/api/v2/incidents":
                total, rows = e.store.list_incidents(
                    parse_filters(q), sort=q.get("sort", "when"), direction=q.get("dir", "desc"),
                    limit=max(1, min(int(q.get("limit", 50)), 200)), offset=max(0, int(q.get("offset", 0))))
                return self._send(200, {"total": total, "items": rows})
            if url.path == "/api/v2/incident":
                d = e.detail(q["id"])
                return self._send(200 if d else 404, d or {"error": "no such entry"})
            if url.path == "/api/v2/stats":
                return self._send(200, e.stats(parse_filters(q), tz_seconds(q)))
            if url.path == "/api/v2/pipelines":
                return self._send(200, {"items": e.store.pipelines(parse_filters(q))})
            if url.path == "/api/v2/pulse":
                since = _num(q.get("since"), 0)
                return self._send(200, {"rev": e.store.rev, "now": time.time(),
                                        "changes": e.store.changes(since) if since else []})
            if url.path == "/api/v2/export":
                data, ctype, name = export.build(e, parse_filters(q), q.get("format", "csv"), q.get("scope", "list"),
                                                 q.get("sort", "when"), q.get("dir", "desc"), tz_seconds(q))
                return self._send_file(data, ctype, name)
            if url.path == "/api/v2/solutions":
                total, rows, counts = e.deck.list(
                    q=q.get("q", ""), status=q.get("status", ""), category=q.get("category", ""),
                    sort=q.get("sort", "seen"), direction=q.get("dir", "desc"),
                    limit=max(1, min(int(q.get("limit", 50)), 500)), offset=max(0, int(q.get("offset", 0))))
                return self._send(200, {"total": total, "items": rows, "counts": counts, "mode": e.deck.mode})
            if url.path == "/api/v2/solution":
                d = e.deck.get(q["id"])
                return self._send(200 if d else 404, d or {"error": "no such card"})
            if url.path == "/api/v2/solutions/match":
                hit = e.deck.match(q.get("text", ""), minimum=_num(q.get("min"), 0.3), usable_only=q.get("all") != "1")
                return self._send(200, {"match": hit})
            if url.path == "/api/v2/solutions/export":
                data, ctype, name = export.build_deck(e, q.get("format", "json"), q.get("status", ""), tz_seconds(q))
                return self._send_file(data, ctype, name)
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
            if path == "/api/v2/solution/update":
                card = e.deck.update(str(body["id"]), body, str(body.get("user") or ""))
                return self._send(200 if card else 404, card or {"error": "no such card"})
            if path == "/api/v2/solution/create":
                return self._send(201, e.deck.create(body, str(body.get("user") or "")))
            if path == "/api/v2/solutions/import":
                return self._send(200, e.deck.import_(body, str(body.get("user") or "")))
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
