"""Incident ids and the incident log files written on the Jenkins instance.

Every analysed failure gets one Markdown file, <BF_LOG_DIR>/incidents/<ID>.md, generated from the Kiro agent's
response: what error was observed, the proposed solution, the assessment and the stats (recurrence, rebuilds, what
the next build did). The file is rewritten when the outcome changes. Each event is also appended as one JSON line to
<BF_LOG_DIR>/incidents.jsonl, for log shippers.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import tempfile
from pathlib import Path

from .errors import fingerprint_lines

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I


def new_id(now: float | None = None) -> str:
    day = dt.datetime.fromtimestamp(now or dt.datetime.now().timestamp()).strftime("%Y%m%d")
    return f"BF-{day}-" + "".join(secrets.choice(ALPHABET) for _ in range(6))


def fingerprint(excerpt: str) -> str | None:
    lines = fingerprint_lines(excerpt)
    return hashlib.sha1("\n".join(lines).encode()).hexdigest()[:16] if lines else None


def _when(ts: float | None) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if ts else "-"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} h"


def outcome_text(inc: dict) -> str:
    if inc.get("next_result") == "SUCCESS":
        how = "a Build Forge rebuild" if inc.get("next_is_rebuild") else "the next build"
        return f"Fixed on the next build (#{inc['next_build']}, {how})"
    if inc.get("fixed_build"):
        return f"Fixed later, in #{inc['fixed_build']} (next build #{inc.get('next_build')} was {inc.get('next_result')})"
    if inc.get("next_build"):
        return f"Still failing (next build #{inc['next_build']} was {inc.get('next_result')})"
    return "Waiting for the next build"


def render(inc: dict, rebuilds: list[dict], recurrence: dict) -> str:
    a = inc.get("analysis") or {}
    stats = [
        ("Seen before (this pipeline)", recurrence.get("in_job", 0)),
        ("Seen before (all pipelines)", recurrence.get("overall", 0)),
        ("Came back after a rebuild", (inc.get("history") or {}).get("rebuilt_and_failed_again", 0)),
        ("Error excerpt sent to Kiro", f"{len(inc.get('excerpt') or '')} of {inc.get('log_chars') or 0} characters"),
        ("Analysis time", _duration(inc.get("seconds"))),
        ("Rebuilds requested", len(rebuilds)),
        ("Outcome", outcome_text(inc)),
    ]
    if inc.get("fixed_at") and inc.get("failed_at"):
        stats.append(("Time to fix", _duration(inc["fixed_at"] - inc["failed_at"])))
    out = [
        f"# {inc['id']}",
        "",
        "| | |", "|---|---|",
        f"| Pipeline | {inc['job']} |",
        f"| Build | #{inc['build']} ({inc.get('result') or 'FAILURE'}) |",
        f"| Build URL | {inc.get('build_url') or '-'} |",
        f"| Failed stage | {inc.get('stage') or '-'} |",
        f"| Node | {inc.get('node') or '-'} |",
        f"| Build finished | {_when(inc.get('failed_at'))} |",
        f"| Analysed | {_when(inc.get('analysed_at'))} by {inc.get('analysed_by') or '-'} |",
        f"| Status | {inc.get('status')} |",
    ]
    if inc.get("rebuild_of"):
        out.append(f"| Rebuild of | #{inc['rebuild_of']} |")
    out += ["", "## Error observed", "", a.get("error_observed") or inc.get("error") or "(no analysis)", "",
            "## Proposed solution", "", a.get("proposed_solution") or "(no analysis)", "",
            "## Assessment", "",
            f"- Category: {a.get('category', '-')}",
            f"- Severity: {a.get('severity', '-')}",
            f"- Confidence: {round(float(a['confidence']) * 100)}%" if "confidence" in a else "- Confidence: -",
            f"- Rebuild with the same parameters can fix it: {'yes' if a.get('rebuild_can_fix') else 'no'}"
            + (f" ({a['rebuild_reason']})" if a.get("rebuild_reason") else ""),
            f"- Error signature: {a.get('signature', '-')}",
            f"- Affected component: {a.get('affected_component') or '-'}",
            "", "## Stats", "", "| | |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in stats]
    if rebuilds:
        out += ["", "## Rebuilds", ""]
        for r in rebuilds:
            out.append(f"- {_when(r['requested_at'])} by {r.get('requested_by') or '?'}: "
                       f"{'#' + str(r['new_build']) if r.get('new_build') else 'queued'}"
                       f"{' -> ' + r['new_result'] if r.get('new_result') else ''}")
    out += ["", "## Error excerpt sent to Kiro", "", "```text", (inc.get("excerpt") or "").replace("```", "'''"), "```", ""]
    return "\n".join(out)


class IncidentLog:
    def __init__(self, log_dir: Path):
        self.dir = log_dir / "incidents"
        self.jsonl = log_dir / "incidents.jsonl"

    def path(self, inc_id: str) -> Path:
        return self.dir / f"{inc_id}.md"

    def write(self, inc: dict, rebuilds: list[dict], recurrence: dict) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.path(inc["id"])
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(render(inc, rebuilds, recurrence))
        os.chmod(tmp, 0o640)
        os.replace(tmp, target)
        return target

    def event(self, kind: str, inc: dict, **extra) -> None:
        a = inc.get("analysis") or {}
        rec = {"ts": dt.datetime.now().astimezone().isoformat(timespec="seconds"), "event": kind, "id": inc["id"],
               "job": inc["job"], "build": inc["build"], "build_url": inc.get("build_url"), "stage": inc.get("stage"),
               "status": inc.get("status"), "category": a.get("category"), "severity": a.get("severity"),
               "confidence": a.get("confidence"), "rebuild_can_fix": a.get("rebuild_can_fix"),
               "signature": a.get("signature"), "error_observed": a.get("error_observed") or inc.get("error"),
               "proposed_solution": a.get("proposed_solution"), "seconds": inc.get("seconds"),
               "next_build": inc.get("next_build"), "next_result": inc.get("next_result"),
               "fixed_build": inc.get("fixed_build"), **extra}
        self.jsonl.parent.mkdir(parents=True, exist_ok=True)
        with open(self.jsonl, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def remove(self, inc_id: str) -> None:
        try:
            self.path(inc_id).unlink()
        except FileNotFoundError:
            pass

    def read(self, inc_id: str) -> str | None:
        try:
            return self.path(inc_id).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
