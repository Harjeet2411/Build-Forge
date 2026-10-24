"""Alerts for the notification bell: a pipeline that keeps failing.

A streak is the number of failed builds of a pipeline in a row, counted from the newest build backwards until a
successful build. Every pipeline is put in an environment from its name (production, UAT/staging, anything else) and
gets a severity when its streak grows past the limit of that environment:

    production pipeline, more than 3 failed builds in a row   -> critical
    UAT / staging pipeline, more than 5                       -> high
    any other pipeline, more than 5                           -> low

A successful build ends the streak and removes the alert (status "resolved"). Admins can dismiss an alert (for
everyone) or mark it read (for themselves). Each alert says who started the failing builds and who committed code
since, so the admin knows whom to contact.
"""
from __future__ import annotations

import json
import secrets
import time

from .config import Settings
from .incidents import ALPHABET

SEVERITIES = ("critical", "high", "low")
RANK = {"critical": 0, "high": 1, "low": 2}
FAILED = ("FAILURE",)
MAX_LOOKBACK = 400  # builds looked at per pipeline


def new_id() -> str:
    return "AL-" + "".join(secrets.choice(ALPHABET) for _ in range(6))


def environment(settings: Settings, job: str) -> str:
    """prod | uat | other, from the pipeline's full name (folder/job/branch)."""
    name = job.lower()
    if settings.env_prod.search(name):
        return "prod"
    if settings.env_uat.search(name):
        return "uat"
    return "other"


def limits(settings: Settings, env: str) -> tuple[str, int]:
    if env == "prod":
        return "critical", settings.alert_prod_after
    if env == "uat":
        return "high", settings.alert_uat_after
    return "low", settings.alert_other_after


def person_key(p: dict) -> str:
    return (p.get("email") or p.get("id") or p.get("name") or "").strip().lower()


def contacts_of(incidents: list[dict]) -> dict:
    """Who to ask. `incidents` are the failed builds of the streak, oldest first.

    - started_by: who or what started the newest failing build, and the first one
    - committers: everyone who committed code in the failing builds, with the builds they appear in; the people behind
      the first failing build come first, since that is the change that most likely broke it
    """
    def trig(i: dict | None) -> dict | None:
        t = (i or {}).get("triggered_by")
        return dict(t, build=i["build"]) if t else None

    people: dict[str, dict] = {}
    for inc in incidents:
        for c in inc.get("commits") or []:
            key = person_key({"email": c.get("email"), "id": c.get("author_id"), "name": c.get("author")})
            if not key:
                continue
            p = people.setdefault(key, {"name": c.get("author") or c.get("author_id") or "", "id": c.get("author_id"),
                                        "email": c.get("email"), "builds": [], "commits": []})
            if not p.get("email") and c.get("email"):
                p["email"] = c["email"]
            if inc["build"] not in p["builds"]:
                p["builds"].append(inc["build"])
            if len(p["commits"]) < 3:
                p["commits"].append({"id": (c.get("id") or "")[:10], "message": c.get("message") or "", "build": inc["build"]})
    first_build = incidents[0]["build"] if incidents else None
    ordered = sorted(people.values(), key=lambda p: (first_build not in p["builds"], p["builds"][0]))
    for p in ordered:
        p["first_failure"] = first_build in p["builds"]
    return {
        "started_latest": trig(incidents[-1] if incidents else None),
        "started_first": trig(incidents[0] if incidents else None),
        "committers": ordered[:8],
    }


class Alerts:
    def __init__(self, store, settings: Settings):
        self.store = store
        self.s = settings

    # ------------------------------------------------------------- evaluation
    def streak(self, job: str) -> tuple[int, list[int], int | None]:
        """(failed builds in a row, their build numbers oldest first, the build that ended the streak or None)."""
        rows = self.store._rows(  # noqa: SLF001
            "SELECT b.build, b.result, (i.id IS NOT NULL) AS has_incident FROM builds b"
            " LEFT JOIN incidents i ON i.job=b.job AND i.build=b.build"
            " WHERE b.job=? ORDER BY b.build DESC LIMIT ?", (job, MAX_LOOKBACK))
        failed: list[int] = []
        for r in rows:
            if r["result"] == "SUCCESS":
                return len(failed), sorted(failed), r["build"]
            if r["result"] in FAILED or r["has_incident"]:
                failed.append(r["build"])
            # aborted / not built / unstable (when not reported as a failure): neither extend nor end the streak
        return len(failed), sorted(failed), None

    def evaluate(self, job: str) -> dict | None:
        """Call after every finished build of `job`. Opens, updates or resolves the alert of that pipeline."""
        if not self.s.alerts:
            return None
        count, builds, ended_by = self.streak(job)
        open_alert = self._current(job)
        if count == 0:
            if open_alert and open_alert["status"] in ("open", "dismissed"):
                self.store._exec(  # noqa: SLF001
                    "UPDATE alerts SET status='resolved', resolved_at=?, resolved_build=?, updated=? WHERE id=?",
                    (time.time(), ended_by, time.time(), open_alert["id"]))
            return None
        env = environment(self.s, job)
        severity, after = limits(self.s, env)
        if count <= after:
            return None
        incidents = [i for i in (self.store.incident_for(job, b) for b in builds) if i]
        failed_at = [i.get("failed_at") or i["created"] for i in incidents]
        fields = {
            "env": env, "severity": severity, "threshold": after, "streak": count,
            "first_build": builds[0], "last_build": builds[-1],
            "first_failed_at": min(failed_at) if failed_at else None,
            "last_failed_at": max(failed_at) if failed_at else None,
            "contacts": json.dumps(contacts_of(incidents)),
        }
        existing = self.store._rows(  # noqa: SLF001
            "SELECT * FROM alerts WHERE job=? AND first_build=? AND status<>'resolved'", (job, builds[0]))
        now = time.time()
        if existing:
            sets = ", ".join(f"{k}=?" for k in fields)
            self.store._exec(f"UPDATE alerts SET {sets}, updated=? WHERE id=?",  # noqa: SLF001
                             (*fields.values(), now, existing[0]["id"]))
            return self.get(existing[0]["id"])
        alert_id = new_id()
        cols = ["id", "job", *fields, "status", "created", "updated"]
        self.store._exec(  # noqa: SLF001
            f"INSERT INTO alerts({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            (alert_id, job, *fields.values(), "open", now, now))
        return self.get(alert_id)

    def _current(self, job: str) -> dict | None:
        rows = self.store._rows(  # noqa: SLF001
            "SELECT * FROM alerts WHERE job=? AND status<>'resolved' ORDER BY first_build DESC LIMIT 1", (job,))
        return rows[0] if rows else None

    def rebuild_all(self) -> int:
        """At start: evaluate every pipeline that has builds (alerts for failures from before the upgrade)."""
        jobs = [r["job"] for r in self.store._rows("SELECT DISTINCT job FROM builds")]  # noqa: SLF001
        n = 0
        for job in jobs:
            if self.evaluate(job):
                n += 1
        return n

    # ------------------------------------------------------------- reads and actions
    def _view(self, row: dict, user: str, read: set[str]) -> dict:
        out = dict(row)
        out["contacts"] = json.loads(row["contacts"]) if row.get("contacts") else {}
        out["read"] = row["id"] in read
        out["after"] = row["threshold"]
        inc = self.store.incident_for(row["job"], row["last_build"])
        out["latest"] = ({"id": inc["id"], "signature": inc.get("signature"), "category": inc.get("category"),
                          "stage": inc.get("stage"), "status": inc["status"], "build_url": inc.get("build_url"),
                          "error": ((inc.get("analysis") or {}).get("error_observed") or inc.get("error") or "")[:300]}
                         if inc else None)
        return out

    def get(self, alert_id: str, user: str = "") -> dict | None:
        rows = self.store._rows("SELECT * FROM alerts WHERE id=?", (alert_id,))  # noqa: SLF001
        return self._view(rows[0], user, self._read_by(user)) if rows else None

    def _read_by(self, user: str) -> set[str]:
        if not user:
            return set()
        return {r["alert_id"] for r in self.store._rows(  # noqa: SLF001
            "SELECT alert_id FROM alert_reads WHERE user=?", (user,))}

    def list(self, user: str = "", status: str = "open") -> dict:
        """status: open (default, what the bell shows) | dismissed | resolved | all."""
        where = {"open": "status='open'", "dismissed": "status='dismissed'", "resolved": "status='resolved'",
                 "all": "1=1"}.get(status, "status='open'")
        rows = self.store._rows(  # noqa: SLF001
            f"SELECT * FROM alerts WHERE {where} ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,"
            " last_failed_at DESC LIMIT 300")
        read = self._read_by(user)
        items = [self._view(r, user, read) for r in rows]
        open_rows = self.store._rows("SELECT id, severity FROM alerts WHERE status='open'")  # noqa: SLF001
        counts = {"open": len(open_rows), "unread": sum(1 for r in open_rows if r["id"] not in read),
                  "dismissed": self.store.scalar("SELECT COUNT(*) FROM alerts WHERE status='dismissed'") or 0}
        for sev in SEVERITIES:
            counts[sev] = sum(1 for r in open_rows if r["severity"] == sev)
            counts["unread_" + sev] = sum(1 for r in open_rows if r["severity"] == sev and r["id"] not in read)
        return {"items": items, "counts": counts, "enabled": self.s.alerts,
                "rules": {"prod": self.s.alert_prod_after, "uat": self.s.alert_uat_after, "other": self.s.alert_other_after}}

    def mark_read(self, user: str, ids: list[str] | None = None, everything: bool = False, unread: bool = False) -> int:
        if not user:
            raise ValueError("user is required")
        if everything:
            ids = [r["id"] for r in self.store._rows("SELECT id FROM alerts WHERE status='open'")]  # noqa: SLF001
        n = 0
        for alert_id in ids or []:
            if unread:
                self.store._exec("DELETE FROM alert_reads WHERE alert_id=? AND user=?", (alert_id, user))  # noqa: SLF001
            else:
                self.store._exec(  # noqa: SLF001
                    "INSERT OR REPLACE INTO alert_reads(alert_id, user, at) VALUES (?,?,?)", (alert_id, user, time.time()))
            n += 1
        return n

    def dismiss(self, alert_id: str, user: str) -> dict | None:
        cur = self.store._exec(  # noqa: SLF001
            "UPDATE alerts SET status='dismissed', dismissed_by=?, dismissed_at=?, updated=? WHERE id=? AND status='open'",
            (user or "?", time.time(), time.time(), alert_id))
        return self.get(alert_id, user) if cur.rowcount or self.get(alert_id) else None

    def restore(self, alert_id: str, user: str) -> dict | None:
        self.store._exec(  # noqa: SLF001
            "UPDATE alerts SET status='open', dismissed_by=NULL, dismissed_at=NULL, updated=? WHERE id=? AND status='dismissed'",
            (time.time(), alert_id))
        return self.get(alert_id, user)

    def prune(self, older_than: float) -> None:
        self.store._exec("DELETE FROM alerts WHERE status='resolved' AND updated<?", (older_than,))  # noqa: SLF001
        self.store._exec("DELETE FROM alert_reads WHERE alert_id NOT IN (SELECT id FROM alerts)")  # noqa: SLF001
