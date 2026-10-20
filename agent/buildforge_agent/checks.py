"""`python3 -m buildforge_agent check`: confirm the runner, the Kiro agent, the log folder and Teams before use."""
from __future__ import annotations

import os
import time

from . import kiro, teams
from .engine import Engine

SAMPLE = {"job": "buildforge-check", "build": 0, "build_url": "", "stage": "Fetch", "node": "built-in", "params": [],
          "error": "+ git fetch origin\nfatal: unable to access 'https://git.example.com/app.git/': "
                   "Could not resolve host: git.example.com\nERROR: script returned exit code 128",
          "history": {}}


def run_checks(engine: Engine, post_teams: bool = False, ask_kiro: bool = True) -> int:
    s, failed = engine.s, 0

    def report(ok: bool | None, what: str, detail: str = "") -> None:
        nonlocal failed
        failed += 0 if ok in (True, None) else 1
        tag = "OK  " if ok else ("SKIP" if ok is None else "FAIL")
        print(f"{tag} {what}{': ' + detail if detail else ''}")

    try:
        engine.logs.dir.mkdir(parents=True, exist_ok=True)
        probe = engine.logs.dir / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        report(True, "Incident log folder", str(engine.logs.dir))
    except OSError as exc:
        report(False, "Incident log folder", f"{engine.logs.dir}: {exc}")

    if not s.runner_url:
        report(False, "Build Forge runner", "BF_RUNNER_URL is not set")
    else:
        try:
            h = kiro.healthz(s.runner_url)
            report(True, "Build Forge runner", f"{s.runner_url} (agent {h.get('agent')}, kiro {h.get('kiro')})")
        except Exception as exc:  # noqa: BLE001
            report(False, "Build Forge runner", f"unreachable at {s.runner_url}: {exc}")
        if ask_kiro and not failed:
            started = time.time()
            try:
                a = kiro.analyze(s.runner_url, s.runner_secret, SAMPLE, s.runner_timeout)["analysis"]
                report(True, "Kiro agent", f"{a.get('category')}, rebuild_can_fix={a.get('rebuild_can_fix')}, "
                       f"{time.time() - started:.0f}s: {str(a.get('error_observed'))[:120]}")
            except Exception as exc:  # noqa: BLE001
                report(False, "Kiro agent", str(exc))
        elif not ask_kiro:
            report(None, "Kiro agent", "skipped (--no-kiro)")

    if s.teams_webhook_url:
        if post_teams:
            try:
                fake = {"id": "BF-TEST", "job": "buildforge-check", "build": 0, "stage": "Fetch",
                        "build_url": "", "analysis": {"category": "infrastructure", "severity": "low",
                                                      "error_observed": "This is a test from `buildforge_agent check`.",
                                                      "proposed_solution": "Nothing to do.", "rebuild_can_fix": False}}
                teams.post(s.teams_webhook_url, teams.incident_card(fake))
                report(True, "Teams webhook", "test card posted")
            except Exception as exc:  # noqa: BLE001
                report(False, "Teams webhook", str(exc))
        else:
            report(None, "Teams webhook", "add --teams to post a test card")
    else:
        report(None, "Teams webhook", "TEAMS_WEBHOOK_URL not set (no Teams alerts)")
    print(f"secret: {'set' if s.shared_secret else 'MISSING'}; database: {s.data_dir}; user: {os.environ.get('USER', '?')}")
    return 1 if failed else 0
