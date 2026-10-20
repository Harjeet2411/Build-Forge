#!/usr/bin/env python3
"""Build Forge runner: one headless Kiro turn per failed Jenkins build.

Runs on the instance where Kiro CLI is installed and logged in. The Jenkins-side Build Forge service POSTs the error
of a failed build to /analyze. The runner starts `kiro-cli chat --no-interactive --agent buildforge-fixer`; the agent
(Claude Opus 4.5) answers through the `submit_analysis` tool of the Build Forge MCP server, which writes the analysis to
a per-run file. The runner returns that structured analysis. Standard library only.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("buildforge.runner")

LISTEN = os.environ.get("BF_RUNNER_LISTEN", "0.0.0.0:8796")
SECRET = os.environ.get("BF_RUNNER_SECRET", "")
AGENT = os.environ.get("BF_AGENT", "buildforge-fixer")
KIRO_BIN = os.environ.get("BF_KIRO_BIN", "kiro-cli")
TIMEOUT = int(os.environ.get("BF_TIMEOUT", "240"))
MAX_ERROR_CHARS = int(os.environ.get("BF_MAX_ERROR_CHARS", "16000"))
SLOTS = threading.Semaphore(int(os.environ.get("BF_MAX_PARALLEL", "3")))
TRUST = os.environ.get("BF_TRUST_TOOLS", "--trust-tools=@buildforge/submit_analysis").split()
# Only these reach kiro-cli (KIRO_API_KEY is how a headless instance logs in).
PASS_ENV = ("PATH", "HOME", "LANG", "KIRO_API_KEY") + tuple(x for x in os.environ.get("BF_PASS_ENV", "").split(",") if x)

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NOISE = re.compile(r"^\s*(?:[●•*-]\s*)?(?:Running tool .*|⋮.*|Completed in [\d.]+m?s.*|"
                   r"[✓✗✔✘❗!]?\s*\d+ of \d+ (?:hooks?|mcp servers?) .*|▸ (?:Credits|Time):.*|One or more mcp server.*|"
                   r"Using tool:.*|-{3,})\s*$", re.I)


def clean(stdout: str) -> str:
    lines = [ln for ln in ANSI.sub("", stdout).replace("\r", "").splitlines() if not NOISE.match(ln)]
    text = "\n".join(lines).strip()
    if text.startswith("> "):
        text = text[2:]
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_prompt(req: dict) -> str:
    error = str(req.get("error", "")).strip()
    if len(error) > MAX_ERROR_CHARS:
        error = error[:2000] + "\n... [truncated] ...\n" + error[-(MAX_ERROR_CHARS - 2000):]
    error = error.replace("</log>", "< /log>")  # the log can't close its own wrapper
    params = ", ".join(map(str, req.get("params") or [])) or "none"
    lines = [f"Pipeline: {req.get('job', '?')}", f"Build: #{req.get('build', '?')}",
             f"Build URL: {req.get('build_url') or '-'}", f"Failed stage: {req.get('stage') or 'unknown'}",
             f"Node: {req.get('node') or 'unknown'}", f"Parameters (names only): {params}"]
    history = req.get("history") or {}
    if history.get("same_error_in_job") or history.get("same_error_overall"):
        lines.append(f"History: this error was seen {history.get('same_error_in_job', 0)} time(s) before in this "
                     f"pipeline and {history.get('same_error_overall', 0)} time(s) overall.")
        if history.get("last_signature"):
            lines.append(f"Label used last time: {history['last_signature']}")
        if history.get("rebuilt_and_failed_again"):
            lines.append(f"It came back after a rebuild {history['rebuilt_and_failed_again']} time(s).")
        if history.get("last_solution"):
            lines.append("Last proposed solution (for reference):\n" + str(history["last_solution"])[:600])
    else:
        lines.append("History: first time this error is seen.")
    return "\n".join(lines) + f"\n\n<log>\n{error}\n</log>\n\nAnalyse it and call submit_analysis."


def run_kiro(prompt: str, result_path: str, workdir: str) -> tuple[int, str, str]:
    env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
    env["BF_RESULT_FILE"] = result_path
    with SLOTS:
        proc = subprocess.run([KIRO_BIN, "chat", "--no-interactive", "--agent", AGENT, "--require-mcp-startup",
                               *TRUST, prompt], capture_output=True, text=True, timeout=TIMEOUT, env=env, cwd=workdir)
    return proc.returncode, proc.stdout, proc.stderr


def analyze(req: dict) -> tuple[int, dict]:
    workdir = tempfile.mkdtemp(prefix="buildforge-")
    result_path = os.path.join(workdir, "analysis.json")
    started = time.time()
    try:
        code, out, err = run_kiro(build_prompt(req), result_path, workdir)
        seconds = round(time.time() - started, 1)
        text = clean(out)
        if os.path.exists(result_path):
            with open(result_path, encoding="utf-8") as fh:
                analysis = json.load(fh)
            return 200, {"analysis": analysis, "seconds": seconds, "agent": AGENT, "text": text[:2000]}
        tail = ANSI.sub("", err)[-400:]
        log.warning("%s#%s: no analysis submitted (exit %s): %s", req.get("job"), req.get("build"), code, tail)
        return 502, {"error": f"the Kiro agent did not submit an analysis (kiro-cli exit {code})",
                     "stderr_tail": tail, "text": text[:500], "seconds": seconds}
    except subprocess.TimeoutExpired:
        return 504, {"error": f"kiro-cli took longer than {TIMEOUT}s"}
    except FileNotFoundError:
        return 500, {"error": f"{KIRO_BIN} not found on this instance (set BF_KIRO_BIN to its full path)"}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "buildforge-runner"

    def log_message(self, fmt, *args):  # noqa: A003
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/healthz":
            return self._send(200, {"ok": True, "agent": AGENT, "kiro": shutil.which(KIRO_BIN) or KIRO_BIN})
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/analyze":
            return self._send(404, {"error": "not found"})
        if not (SECRET and hmac.compare_digest(self.headers.get("X-BuildForge-Secret", ""), SECRET)):
            return self._send(401, {"error": "missing or wrong X-BuildForge-Secret"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 512 * 1024:
                return self._send(413, {"error": "request too large"})
            req = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(req, dict) or not str(req.get("error", "")).strip():
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "JSON body with job, build and error is required"})
        code, body = analyze(req)
        if code == 200:
            log.info("%s#%s: analysed in %ss (%s, rebuild_can_fix=%s)", req.get("job"), req.get("build"),
                     body["seconds"], body["analysis"].get("category"), body["analysis"].get("rebuild_can_fix"))
        self._send(code, body)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not SECRET:
        raise SystemExit("BF_RUNNER_SECRET is not set; refusing to start without it")
    host, _, port = LISTEN.rpartition(":")
    httpd = ThreadingHTTPServer((host or "0.0.0.0", int(port or 8796)), Handler)
    httpd.daemon_threads = True
    log.info("Build Forge runner on %s (agent=%s)", LISTEN, AGENT)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
