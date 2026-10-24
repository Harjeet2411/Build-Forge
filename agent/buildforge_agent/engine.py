"""What happens to a failed build: record an incident, ask the Kiro agent, write the incident log, alert Teams,
and follow the next builds of the job to learn whether (and how) the failure was fixed."""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import errors, kiro, teams
from .config import Settings
from .alerts import Alerts
from .deck import Deck
from .incidents import IncidentLog, fingerprint, new_id
from .store import Store

log = logging.getLogger("buildforge.engine")

ANALYSIS_FIELDS = ("error_observed", "proposed_solution", "category", "severity", "confidence", "rebuild_can_fix",
                   "rebuild_reason", "signature", "affected_component")


def _seconds(ms) -> float | None:
    try:
        return float(ms) / 1000.0 if ms else None
    except (TypeError, ValueError):
        return None


def _text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def clean_trigger(raw) -> dict | None:
    """{kind, id, name} of whoever or whatever started the build (the plugin sends it; nothing here is trusted as markup)."""
    if not isinstance(raw, dict):
        return None
    out = {k: _text(raw.get(k), 120) for k in ("kind", "id", "name")}
    return out if any(out.values()) else None


def clean_commits(raw) -> list[dict]:
    out = []
    for c in (raw if isinstance(raw, list) else [])[:100]:
        if not isinstance(c, dict):
            continue
        item = {"id": _text(c.get("id"), 40), "author": _text(c.get("author"), 120), "author_id": _text(c.get("author_id"), 120),
                "email": _text(c.get("email"), 160), "message": _text(c.get("message"), 200).splitlines()[0] if c.get("message") else ""}
        if item["author"] or item["author_id"] or item["email"]:
            out.append(item)
    return out[:20]


def admin_url(build_url: str | None, inc_id: str) -> str:
    """https://jenkins/job/a/job/b/12/ -> https://jenkins/buildforge/admin#BF-..."""
    if not build_url or "/job/" not in build_url:
        return ""
    return build_url.split("/job/", 1)[0].rstrip("/") + "/buildforge/admin#" + inc_id


class Engine:
    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self.logs = IncidentLog(settings.log_dir)
        self.deck = Deck(store, settings.deck_mode, settings.deck_threshold, settings.deck_first_threshold,
                         settings.deck_min_seen)
        self.alerts = Alerts(store, settings)
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="buildforge")
        self._outcome_lock = threading.Lock()
        self._busy = threading.Condition()
        self._active = 0  # analyses queued or running

    # failures -------------------------------------------------------------
    def submit(self, body: dict) -> dict:
        """A failed build from the plugin. Returns at once; the analysis runs in the background."""
        job, build = str(body["job"]), int(body["build"])
        result = str(body.get("result") or "FAILURE")
        rebuild_of = int(body["rebuild_of"]) if str(body.get("rebuild_of") or "").isdigit() else None
        finished = (_seconds(body.get("timestamp")) or time.time()) + (_seconds(body.get("duration_ms")) or 0)
        self.build_result(job, build, result, rebuild_of, finished)
        # Every failure is recorded for the dashboard. The job filters (plugin and BF_JOB_INCLUDE / BF_JOB_EXCLUDE)
        # only decide whether Kiro analyses it.
        analyse = body.get("analyse") is not False and self.s.job_allowed(job)
        existing = self.store.incident_for(job, build)
        if existing:
            return {"ok": True, "id": existing["id"], "status": existing["status"], "duplicate": True}

        console = str(body.get("log") or "")
        part = errors.extract(console, max_chars=self.s.max_error_chars)
        excerpt = part["text"] if self.s.send != "full" else errors.clean_log(console)[-self.s.max_error_chars:]
        inc = {
            "id": new_id(),
            "job": job, "build": build, "build_url": body.get("build_url") or body.get("url"),
            "result": result, "status": "queued" if analyse else "skipped",
            "error": None if analyse else "Not analysed: this pipeline is excluded from analysis (Build Forge job filter)",
            "stage": part["stage"], "node": part["node"] or body.get("node"),
            "stages": errors.stages(console, part["stage"]),
            "params": [str(p) for p in (body.get("params") or [])][:50],
            "rebuild_of": rebuild_of,
            "fingerprint": fingerprint(part["text"]),
            "excerpt": excerpt or "(the console log was empty)",
            "log_chars": len(console),
            "failed_at": finished,
            "triggered_by": clean_trigger(body.get("triggered_by")),
            "commits": clean_commits(body.get("commits")) or None,
        }
        if not self.store.create_incident(inc):  # a retry raced us
            existing = self.store.incident_for(job, build)
            return {"ok": True, "id": existing["id"], "status": existing["status"], "duplicate": True}
        self.evaluate_alerts(job)
        if not analyse:
            log.info("%s#%s: incident %s recorded, not analysed (job filter)", job, build, inc["id"])
            self._write(self.store.incident(inc["id"]), "recorded")
            return {"ok": True, "id": inc["id"], "status": "skipped"}
        log.info("%s#%s: incident %s (stage %s, %d of %d chars to Kiro)", job, build, inc["id"], inc["stage"],
                 len(excerpt), len(console))
        self._queue(inc["id"])
        return {"ok": True, "id": inc["id"], "status": "queued"}

    def evaluate_alerts(self, job: str) -> None:
        try:
            self.alerts.evaluate(job)
        except Exception:  # noqa: BLE001 - an alert problem must never lose a failure
            log.exception("could not evaluate the alerts of %s", job)

    def _queue(self, inc_id: str) -> None:
        with self._busy:
            self._active += 1
        self._pool.submit(self._safe_process, inc_id)

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until no analysis is queued or running (used by the tests and by a clean shutdown)."""
        with self._busy:
            return self._busy.wait_for(lambda: self._active == 0, timeout)

    def _safe_process(self, inc_id: str) -> None:
        try:
            self.process(inc_id)
        except Exception:  # noqa: BLE001 - never lose the worker thread
            log.exception("analysis of %s crashed", inc_id)
            self.store.update_incident(inc_id, status="error", error="Build Forge crashed while analysing; see its log")
        finally:
            with self._busy:
                self._active -= 1
                self._busy.notify_all()

    def process(self, inc_id: str) -> None:
        inc = self.store.incident(inc_id)
        if not inc:
            return
        history = (self.store.history(inc["fingerprint"], inc["job"], inc_id, bool(inc["rebuild_of"]))
                   if inc["fingerprint"] else {})
        self.store.update_incident(inc_id, status="analysing", history=history, error=None)
        request = {"job": inc["job"], "build": inc["build"], "build_url": inc["build_url"], "stage": inc["stage"],
                   "node": inc["node"], "params": inc["params"] or [], "error": inc["excerpt"],
                   "history": {k: v for k, v in history.items() if k != "last_id"}}
        from_deck = self.deck.answer(inc["excerpt"], inc["fingerprint"], history, first=True)
        if from_deck:
            self._finish_from_deck(inc_id, from_deck, 0.0)
        else:
            try:
                answer = kiro.analyze(self.s.runner_url, self.s.runner_secret, request, self.s.runner_timeout)
                a = {k: answer["analysis"].get(k) for k in ANALYSIS_FIELDS if answer["analysis"].get(k) is not None}
                self.store.update_incident(
                    inc_id, status="done", analysis=a, signature=a.get("signature"), category=a.get("category"),
                    rebuild_can_fix=1 if a.get("rebuild_can_fix") else 0, seconds=answer.get("seconds"),
                    analysed_at=time.time(), analysed_by=f"Kiro agent {answer.get('agent') or 'buildforge-fixer'}")
                log.info("%s: %s, rebuild_can_fix=%s (%ss)", inc_id, a.get("category"), a.get("rebuild_can_fix"),
                         answer.get("seconds"))
            except kiro.RunnerError as exc:
                log.warning("%s: %s", inc_id, exc)
                fallback = self.deck.answer(inc["excerpt"], inc["fingerprint"], history)
                if fallback:
                    log.info("%s: Kiro unavailable, answered from the solutions deck (%s)", inc_id, fallback["deck_id"])
                    self._finish_from_deck(inc_id, fallback, 0.0, kiro_error=str(exc))
                else:
                    self.store.update_incident(inc_id, status="error", error=str(exc), analysed_at=time.time())
        inc = self.store.incident(inc_id)
        self._write(inc, "analysed" if inc["status"] == "done" else "analysis_failed")
        self._maybe_notify(inc)
        try:
            self.deck.record(inc)
        except Exception:  # noqa: BLE001 - the deck must never break an analysis
            log.exception("could not file %s in the solutions deck", inc_id)

    def _finish_from_deck(self, inc_id: str, analysis: dict, seconds: float, kiro_error: str | None = None) -> None:
        if kiro_error:
            analysis = dict(analysis, kiro_error=kiro_error[:300])
        self.store.update_incident(
            inc_id, status="done", analysis=analysis, signature=analysis.get("signature"),
            category=analysis.get("category"), rebuild_can_fix=1 if analysis.get("rebuild_can_fix") else 0,
            seconds=seconds, analysed_at=time.time(),
            analysed_by=f"Solutions deck {analysis['deck_id']} ({round(analysis['deck_match'] * 100)}% match)")

    def _write(self, inc: dict, event: str | None = None, **extra) -> None:
        try:
            self.logs.write(inc, self.store.rebuilds_of(inc["job"], inc["build"]), self.store.recurrence(inc))
            if event:
                self.logs.event(event, inc, **extra)
        except OSError:
            log.exception("could not write the incident log of %s", inc["id"])

    def _maybe_notify(self, inc: dict) -> None:
        cat = (inc.get("analysis") or {}).get("category")
        if inc["status"] != "done" or inc.get("notified") or not self.s.teams_webhook_url:
            return
        if cat not in self.s.notify_on and "all" not in self.s.notify_on:
            return
        try:
            teams.post(self.s.teams_webhook_url, teams.incident_card(inc, admin_url(inc.get("build_url"), inc["id"])))
            self.store.update_incident(inc["id"], notified=1)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: Teams alert failed: %s", inc["id"], exc)

    def reanalyze(self, inc_id: str) -> dict:
        inc = self.store.incident(inc_id)
        if not inc:
            return {"error": "no such entry"}
        if inc["status"] in ("queued", "analysing"):
            return {"ok": True, "status": inc["status"]}
        self.store.update_incident(inc_id, status="queued", error=None)  # also lifts "excluded": a person asked
        self._queue(inc_id)
        return {"ok": True, "status": "queued"}

    def resume_pending(self) -> None:
        for inc in self.store.pending():
            log.info("resuming analysis of %s", inc["id"])
            self._queue(inc["id"])

    # outcomes -------------------------------------------------------------
    def build_result(self, job: str, build: int, result: str, rebuild_of: int | None, finished: float) -> None:
        """Every completed build (green ones too): fills in "fixed on the next build" for earlier failures."""
        with self._outcome_lock:
            self.store.record_build(job, build, result, rebuild_of, finished)
            if rebuild_of:
                if not any(r.get("new_build") == build for r in self.store.rebuilds_of(job, rebuild_of)):
                    self.store.rebuild_started(job, rebuild_of, build)
                self.store.rebuild_finished(job, build, result)
            for inc in self.store.open_incidents_of(job, build):
                changes = {}
                if inc["next_build"] is None or inc["next_build"] > build:
                    changes.update(next_build=build, next_result=result,
                                   next_is_rebuild=1 if rebuild_of == inc["build"] else 0)
                if result == "SUCCESS" and (inc["fixed_build"] is None or inc["fixed_build"] > build):
                    changes.update(fixed_build=build, fixed_at=finished)
                if changes:
                    self.store.update_incident(inc["id"], **changes)
                    if inc["status"] in ("done", "error", "skipped"):
                        self._write(self.store.incident(inc["id"]), "outcome", trigger_build=build,
                                    trigger_result=result)

    def rebuild_event(self, body: dict) -> None:
        job, origin = str(body["job"]), int(body["build"])
        inc = self.store.incident_for(job, origin)
        if body.get("phase") == "started" and str(body.get("new_build") or "").isdigit():
            self.store.rebuild_started(job, origin, int(body["new_build"]))
        else:
            self.store.add_rebuild(inc and inc["id"], job, origin, str(body.get("user") or "?"))
        if inc and inc["status"] in ("done", "error", "skipped"):
            self._write(self.store.incident(inc["id"]), "rebuild", phase=body.get("phase") or "requested",
                        user=body.get("user"), new_build=body.get("new_build"))

    # reads ----------------------------------------------------------------
    def view(self, job: str, build: int) -> dict | None:
        """What the panel on the failed build shows."""
        inc = self.store.incident_for(job, build)
        if not inc:
            return None
        return {
            "id": inc["id"], "status": inc["status"], "job": job, "build": build, "result": inc["result"],
            "stage": inc["stage"], "stages": inc["stages"] or [], "node": inc["node"],
            "analysis": inc["analysis"], "error": inc["error"], "seconds": inc["seconds"],
            "analysed_by": inc["analysed_by"], "rebuild_of": inc["rebuild_of"],
            "recurrence": self.store.recurrence(inc),
            "rebuilds": self.store.rebuilds_of(job, build),
            "next_build": inc["next_build"], "next_result": inc["next_result"], "fixed_build": inc["fixed_build"],
            "updated": inc["updated"],
        }

    def detail(self, inc_id: str) -> dict | None:
        inc = self.store.incident(inc_id)
        if not inc:
            return None
        related = []
        if inc["fingerprint"]:
            related = [r for r in self.store._rows(  # noqa: SLF001
                "SELECT id, job, build, build_url, next_result, created FROM incidents WHERE fingerprint=? AND id<>?"
                " ORDER BY created DESC LIMIT 10", (inc["fingerprint"], inc_id))]
        inc["recurrence"] = self.store.recurrence(inc)
        inc.pop("fingerprint", None)
        inc["rebuilds"] = self.store.rebuilds_of(inc["job"], inc["build"])
        inc["related"] = related
        inc["log_file"] = str(self.logs.path(inc_id))
        inc["log_text"] = self.logs.read(inc_id)
        return inc

    def stats(self, f: dict, tz: int = 0) -> dict:
        out = self.store.stats(f, tz)
        out["runner"] = self.s.runner_url
        out["log_dir"] = str(self.logs.dir)
        out["deck_mode"] = self.deck.mode
        return out

    def prune(self) -> int:
        if self.s.retention_days <= 0:
            return 0
        self.alerts.prune(time.time() - self.s.retention_days * 86400)
        ids = self.store.prune(time.time() - self.s.retention_days * 86400)
        for inc_id in ids:
            self.logs.remove(inc_id)
        return len(ids)
