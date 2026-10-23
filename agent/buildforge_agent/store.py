"""SQLite store: one incident per failed build, every completed build (for "fixed on the next build"), and rebuilds."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

# The moment a failure happened: when the build finished (falls back to when Build Forge heard of it).
TS = "COALESCE(failed_at, created)"
OUTCOME_ORDER = ("CASE WHEN next_result='SUCCESS' THEN 0 WHEN fixed_build IS NOT NULL THEN 1"
                 " WHEN next_build IS NOT NULL THEN 2 ELSE 3 END")
SORTS = {
    "when": TS, "id": "id", "job": "job", "build": "build", "stage": "stage", "category": "category",
    "analysis": "status", "outcome": OUTCOME_ORDER, "seconds": "seconds", "signature": "signature",
    "result": "result", "node": "node",
}
ANALYSIS_STATES = {
    "done": "status='done'", "error": "status='error'", "skipped": "status='skipped'",
    "progress": "status IN ('queued','analysing')",
}
LIST_COLUMNS = ("id, job, build, build_url, result, status, stage, node, signature, category, rebuild_can_fix,"
                " analysis, error, seconds, next_build, next_result, next_is_rebuild, fixed_build, fixed_at, failed_at,"
                " created, updated")


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like(text: str) -> str:
    return "%" + _esc(text) + "%"


def _glob(pattern: str) -> str:
    """'payments/*' -> a LIKE pattern: every other character matches literally."""
    return "%".join(_esc(part) for part in pattern.split("*"))


def order_by(sort: str, direction: str) -> str:
    col = SORTS.get(sort, TS)
    d = "ASC" if str(direction).lower() == "asc" else "DESC"
    return f"({col} IS NULL), {col} {d}, {TS} DESC, id"


def where(f: dict) -> tuple[str, list]:
    """The WHERE clause (without the word WHERE) for the dashboard filters; the same one drives the list, the numbers
    on the cards and the exports, so what you see is what you download.
    Keys: since, until (epoch seconds, until is exclusive), q, jobs (names, '*' is a wildcard), categories, outcome,
    analysis (done|error|skipped|progress), rebuild (yes|no), result (FAILURE|UNSTABLE)."""
    w, a = ["1=1"], []
    if f.get("since"):
        w.append(f"{TS}>=?")
        a.append(float(f["since"]))
    if f.get("until"):
        w.append(f"{TS}<?")
        a.append(float(f["until"]))
    q = (f.get("q") or "").strip()
    if q:
        w.append("(id LIKE ? ESCAPE '\\' OR job LIKE ? ESCAPE '\\' OR signature LIKE ? ESCAPE '\\'"
                 " OR stage LIKE ? ESCAPE '\\' OR node LIKE ? ESCAPE '\\' OR analysis LIKE ? ESCAPE '\\'"
                 " OR error LIKE ? ESCAPE '\\' OR excerpt LIKE ? ESCAPE '\\')")
        a += [_like(q)] * 8
    jobs = [j for j in (f.get("jobs") or []) if j]
    if jobs:
        parts = []
        for j in jobs:
            if "*" in j:  # my-app/* , *-prod
                parts.append("job LIKE ? ESCAPE '\\'")
                a.append(_glob(j))
            else:
                parts.append("job=?")
                a.append(j)
        w.append("(" + " OR ".join(parts) + ")")
    cats = [c for c in (f.get("categories") or []) if c]
    if cats:
        known = [c for c in cats if c not in ("pending", "excluded", "not_analysed")]
        parts = []
        if known:
            parts.append("category IN (" + ",".join("?" * len(known)) + ")")
            a += known
        if "excluded" in cats:
            parts.append("status='skipped'")
        if "not_analysed" in cats:
            parts.append("status='error'")
        if "pending" in cats:
            parts.append("status IN ('queued','analysing')")
        w.append("(" + " OR ".join(parts) + ")")
    out = f.get("outcome") or ""
    if out == "fixed-next":
        w.append("next_result='SUCCESS'")
    elif out == "fixed-later":
        w.append("fixed_build IS NOT NULL AND (next_result IS NULL OR next_result<>'SUCCESS')")
    elif out == "open":
        w.append("fixed_build IS NULL")
    elif out == "fixed":
        w.append("fixed_build IS NOT NULL")
    elif out == "rebuilt":
        w.append("EXISTS (SELECT 1 FROM rebuilds r WHERE r.incident_id=incidents.id)")
    state = ANALYSIS_STATES.get(f.get("analysis") or "")
    if state:
        w.append(state)
    if f.get("rebuild") == "yes":
        w.append("rebuild_can_fix=1")
    elif f.get("rebuild") == "no":
        w.append("(rebuild_can_fix=0 AND status='done')")
    if f.get("result"):
        w.append("result=?")
        a.append(str(f["result"]).upper())
    return " AND ".join(w), a

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,               -- BF-YYYYMMDD-XXXXXX, the unique entry id of this error and its response
    job TEXT NOT NULL,
    build INTEGER NOT NULL,
    build_url TEXT,
    result TEXT,
    status TEXT NOT NULL,              -- queued | analysing | done | error
    stage TEXT,
    node TEXT,
    stages TEXT,                       -- JSON [{name, status}]
    params TEXT,                       -- JSON [names]
    rebuild_of INTEGER,                -- set when this build was started by a Build Forge rebuild of that build
    fingerprint TEXT,                  -- hash of the error lines with numbers blanked (finds the same error again)
    excerpt TEXT,                      -- what was sent to Kiro
    log_chars INTEGER,
    history TEXT,                      -- JSON, what Kiro was told about earlier occurrences
    analysis TEXT,                     -- JSON, what the Kiro agent submitted
    signature TEXT,
    category TEXT,
    rebuild_can_fix INTEGER,
    analysed_by TEXT,
    analysed_at REAL,
    seconds REAL,
    error TEXT,                        -- why there is no analysis (runner down, timeout...)
    failed_at REAL,                    -- when the build finished
    next_build INTEGER,                -- the next build of the same job after this one...
    next_result TEXT,                  -- ...and how it ended
    next_is_rebuild INTEGER,           -- 1 when that next build was a Build Forge rebuild of this one
    fixed_build INTEGER,               -- first later build of the job that succeeded
    fixed_at REAL,
    notified INTEGER DEFAULT 0,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    UNIQUE (job, build)
);
CREATE INDEX IF NOT EXISTS incidents_created ON incidents(created);
CREATE INDEX IF NOT EXISTS incidents_fp ON incidents(fingerprint);
CREATE TABLE IF NOT EXISTS builds (
    job TEXT NOT NULL,
    build INTEGER NOT NULL,
    result TEXT,
    rebuild_of INTEGER,
    finished REAL,
    PRIMARY KEY (job, build)
);
CREATE TABLE IF NOT EXISTS rebuilds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT,
    job TEXT NOT NULL,
    origin_build INTEGER NOT NULL,
    requested_by TEXT,
    requested_at REAL NOT NULL,
    new_build INTEGER,
    new_result TEXT
);
CREATE INDEX IF NOT EXISTS rebuilds_origin ON rebuilds(job, origin_build);
CREATE TABLE IF NOT EXISTS solutions (       -- the solutions deck: one reusable answer per kind of error
    id TEXT PRIMARY KEY,                     -- SD-XXXXXX
    signature TEXT,                          -- the Kiro agent's stable label for the error
    title TEXT NOT NULL,
    category TEXT,
    error_observed TEXT,
    proposed_solution TEXT,
    rebuild_can_fix INTEGER DEFAULT 0,
    rebuild_reason TEXT,
    confidence REAL,
    pattern TEXT,                            -- the normalised error lines this answer was written for
    tokens TEXT,                             -- JSON list of words in the pattern (used for similarity)
    fingerprints TEXT,                       -- JSON list of error fingerprints known to belong to this answer
    status TEXT NOT NULL DEFAULT 'draft',    -- draft | approved | retired
    source TEXT DEFAULT 'auto',              -- auto (learned from Kiro) | manual | import
    notes TEXT,
    edited_by TEXT,
    last_incident TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS solutions_signature ON solutions(signature);
"""

JSON_COLS = ("stages", "params", "history", "analysis")


def _decode(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col in JSON_COLS:
        if out.get(col):
            out[col] = json.loads(out[col])
    return out


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.rev = 0  # bumped on every write; the dashboard polls it to refresh when something changed
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            have = {r[1] for r in self._db.execute("PRAGMA table_info(incidents)")}
            for col, kind in (("analysed_at", "REAL"),):  # columns added after 2.0.0 databases were created
                if col not in have:
                    self._db.execute(f"ALTER TABLE incidents ADD COLUMN {col} {kind}")
            self._db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            self.rev += 1
            return cur

    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [_decode(r) for r in self._db.execute(sql, args).fetchall()]

    def scalar(self, sql: str, args: tuple = ()):
        with self._lock:
            row = self._db.execute(sql, args).fetchone()
            return row[0] if row else None

    # incidents ------------------------------------------------------------
    def create_incident(self, inc: dict) -> bool:
        """False when this build already has an incident (the plugin retried)."""
        now = time.time()
        inc = dict(inc, created=now, updated=now)
        cols = list(inc)
        vals = [json.dumps(inc[c]) if c in JSON_COLS and inc[c] is not None else inc[c] for c in cols]
        try:
            self._exec(f"INSERT INTO incidents({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(vals))
            return True
        except sqlite3.IntegrityError:
            return False

    def update_incident(self, inc_id: str, **fields) -> None:
        cols, args = [], []
        for key, value in fields.items():
            cols.append(f"{key}=?")
            args.append(json.dumps(value) if key in JSON_COLS and value is not None else value)
        cols.append("updated=?")
        args.append(time.time())
        self._exec(f"UPDATE incidents SET {', '.join(cols)} WHERE id=?", (*args, inc_id))

    def incident(self, inc_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM incidents WHERE id=?", (inc_id,))
        return rows[0] if rows else None

    def incident_for(self, job: str, build: int) -> dict | None:
        rows = self._rows("SELECT * FROM incidents WHERE job=? AND build=?", (job, build))
        return rows[0] if rows else None

    def pending(self) -> list[dict]:
        return self._rows("SELECT * FROM incidents WHERE status IN ('queued','analysing') ORDER BY created")

    def history(self, fingerprint: str, job: str, before: str, is_rebuild: bool = False) -> dict:
        """Earlier incidents with the same error, for the prompt. A failed Build Forge rebuild counts as 'came back'."""
        rows = self._rows("SELECT id, job, signature, analysis, rebuild_of, created FROM incidents"
                          " WHERE fingerprint=? AND id<>? AND status='done' ORDER BY created DESC", (fingerprint, before))
        same_job = [r for r in rows if r["job"] == job]
        last = rows[0] if rows else None
        return {
            "same_error_in_job": len(same_job),
            "same_error_overall": len(rows),
            "last_signature": last and last.get("signature"),
            "rebuilt_and_failed_again": sum(1 for r in same_job if r.get("rebuild_of")) + (1 if is_rebuild and same_job else 0),
            "last_solution": last and (last.get("analysis") or {}).get("proposed_solution"),
            "last_id": last and last["id"],
        }

    def recurrence(self, inc: dict) -> dict:
        """How many times the same error was seen BEFORE this incident (in this job, and anywhere)."""
        if not inc.get("fingerprint"):
            return {"in_job": 0, "overall": 0}
        args = (inc["fingerprint"], inc["id"], inc["created"])
        return {
            "in_job": self.scalar("SELECT COUNT(*) FROM incidents WHERE fingerprint=? AND id<>? AND created<? AND job=?",
                                  (*args, inc["job"])) or 0,
            "overall": self.scalar("SELECT COUNT(*) FROM incidents WHERE fingerprint=? AND id<>? AND created<?",
                                   args) or 0,
        }

    def list_incidents(self, f: dict, sort: str = "when", direction: str = "desc", limit: int = 50,
                       offset: int = 0) -> tuple[int, list[dict]]:
        w, args = where(f)
        total = self.scalar(f"SELECT COUNT(*) FROM incidents WHERE {w}", tuple(args)) or 0
        rows = self._rows(f"SELECT {LIST_COLUMNS} FROM incidents WHERE {w} ORDER BY {order_by(sort, direction)}"
                          " LIMIT ? OFFSET ?", (*args, limit, offset))
        return total, rows

    def export_rows(self, f: dict, sort: str = "when", direction: str = "desc", limit: int = 50000) -> list[dict]:
        """Every column the reports need, for the same filter the list uses."""
        w, args = where(f)
        return self._rows(f"SELECT * FROM incidents WHERE {w} ORDER BY {order_by(sort, direction)} LIMIT ?",
                          (*args, limit))

    def pipelines(self, f: dict) -> list[dict]:
        """Every pipeline with failures (for the pipeline picker), most failures first."""
        w, args = where(dict(f, jobs=None))
        return self._rows(f"SELECT job, COUNT(*) AS failed, MAX({TS}) AS last_failed,"
                          f" SUM(fixed_build IS NULL) AS open FROM incidents WHERE {w} GROUP BY job"
                          " ORDER BY failed DESC, job LIMIT 2000", tuple(args))

    def changes(self, since: float, limit: int = 20) -> list[dict]:
        """Incidents touched after `since` (new failures, analyses that finished, outcomes), newest first."""
        return self._rows("SELECT id, job, build, status, category, stage, created, updated, next_result,"
                          " fixed_build FROM incidents WHERE updated>? ORDER BY updated DESC LIMIT ?", (since, limit))

    def open_incidents_of(self, job: str, before_build: int) -> list[dict]:
        return self._rows("SELECT * FROM incidents WHERE job=? AND build<? AND"
                          " (fixed_build IS NULL OR fixed_build>? OR next_build IS NULL OR next_build>?)",
                          (job, before_build, before_build, before_build))

    def prune(self, older_than: float) -> list[str]:
        ids = [r["id"] for r in self._rows("SELECT id FROM incidents WHERE created<?", (older_than,))]
        self._exec("DELETE FROM incidents WHERE created<?", (older_than,))
        self._exec("DELETE FROM builds WHERE finished<?", (older_than,))
        self._exec("DELETE FROM rebuilds WHERE requested_at<?", (older_than,))
        return ids

    # builds ---------------------------------------------------------------
    def record_build(self, job: str, build: int, result: str, rebuild_of: int | None, finished: float) -> None:
        self._exec("INSERT OR REPLACE INTO builds(job, build, result, rebuild_of, finished) VALUES (?,?,?,?,?)",
                   (job, build, result, rebuild_of, finished))

    # rebuilds -------------------------------------------------------------
    def add_rebuild(self, incident_id: str | None, job: str, origin_build: int, user: str) -> int:
        cur = self._exec("INSERT INTO rebuilds(incident_id, job, origin_build, requested_by, requested_at)"
                         " VALUES (?,?,?,?,?)", (incident_id, job, origin_build, user, time.time()))
        return int(cur.lastrowid)

    def rebuild_started(self, job: str, origin_build: int, new_build: int) -> None:
        with self._lock:
            row = self._db.execute("SELECT id FROM rebuilds WHERE job=? AND origin_build=? AND new_build IS NULL"
                                   " ORDER BY id LIMIT 1", (job, origin_build)).fetchone()
            if row:
                self._db.execute("UPDATE rebuilds SET new_build=? WHERE id=?", (new_build, row[0]))
            else:  # requested before Build Forge was told (or the request event was lost)
                self._db.execute("INSERT INTO rebuilds(incident_id, job, origin_build, requested_by, requested_at,"
                                 " new_build) VALUES ((SELECT id FROM incidents WHERE job=? AND build=?),?,?,?,?,?)",
                                 (job, origin_build, job, origin_build, "?", time.time(), new_build))
            self._db.commit()
            self.rev += 1

    def rebuild_finished(self, job: str, new_build: int, result: str) -> None:
        self._exec("UPDATE rebuilds SET new_result=? WHERE job=? AND new_build=?", (result, job, new_build))

    def rebuilds_of(self, job: str, origin_build: int) -> list[dict]:
        return self._rows("SELECT * FROM rebuilds WHERE job=? AND origin_build=? ORDER BY id", (job, origin_build))

    # stats ----------------------------------------------------------------
    def stats(self, f: dict, tz: int = 0) -> dict:
        """Numbers for the cards and charts. `f` is the dashboard filter; `tz` is the viewer's UTC offset in seconds,
        so "per day" means the viewer's days."""
        w, args = where(f)
        args = tuple(args)
        base = f"FROM incidents WHERE {w}"

        def one(sql: str, *extra):
            return self.scalar(sql, (*args, *extra)) or 0

        total = one(f"SELECT COUNT(*) {base}")
        fixed_next = one(f"SELECT COUNT(*) {base} AND next_result='SUCCESS'")
        fixed_later = one(f"SELECT COUNT(*) {base} AND fixed_build IS NOT NULL AND"
                          " (next_result IS NULL OR next_result<>'SUCCESS')")
        lo = f.get("since") or self.scalar(f"SELECT MIN({TS}) FROM incidents") or time.time()
        hi = f.get("until") or time.time()
        bucket = "day" if hi - lo <= 92 * 86400 else "month"
        fmt = "%Y-%m-%d" if bucket == "day" else "%Y-%m"
        with self._lock:
            by_cat = {r[0]: r[1] for r in self._db.execute(
                "SELECT COALESCE(category, CASE status WHEN 'skipped' THEN 'excluded' WHEN 'error' THEN 'not_analysed'"
                f" ELSE 'pending' END), COUNT(*) {base} GROUP BY 1 ORDER BY 2 DESC", args)}
            daily = [{"day": r[0], "failed": r[1], "fixed_next": r[2] or 0} for r in self._db.execute(
                f"SELECT strftime('{fmt}', {TS}+?, 'unixepoch'), COUNT(*), SUM(next_result='SUCCESS') {base}"
                " GROUP BY 1 ORDER BY 1", (tz, *args))]
            top_sig = [{"signature": r[0], "count": r[1], "pipelines": r[2], "last_seen": r[3], "fixed": r[4] or 0,
                        "last_id": r[5]} for r in self._db.execute(
                f"SELECT signature, COUNT(*), COUNT(DISTINCT job), MAX({TS}), SUM(fixed_build IS NOT NULL),"
                f" (SELECT i2.id FROM incidents i2 WHERE i2.signature=incidents.signature ORDER BY i2.created DESC LIMIT 1)"
                f" {base} AND signature IS NOT NULL GROUP BY signature ORDER BY 2 DESC, 4 DESC LIMIT 10", args)]
            top_jobs = [{"job": r[0], "failed": r[1], "fixed_next": r[2] or 0, "open": r[3] or 0} for r in
                        self._db.execute(f"SELECT job, COUNT(*), SUM(next_result='SUCCESS'), SUM(fixed_build IS NULL)"
                                         f" {base} GROUP BY job ORDER BY 2 DESC, 1 LIMIT 10", args)]
            fix_times = sorted(r[0] for r in self._db.execute(
                f"SELECT fixed_at - failed_at {base} AND fixed_at IS NOT NULL AND failed_at IS NOT NULL", args))
        inc_ids = f"incident_id IN (SELECT id {base})"
        rebuilds = one(f"SELECT COUNT(*) FROM rebuilds WHERE {inc_ids}")
        rebuild_ok = one(f"SELECT COUNT(*) FROM rebuilds WHERE {inc_ids} AND new_result='SUCCESS'")
        rebuild_bad = one(f"SELECT COUNT(*) FROM rebuilds WHERE {inc_ids} AND new_result IS NOT NULL"
                          " AND new_result<>'SUCCESS'")
        bw, ba = ["finished IS NOT NULL"], []
        if f.get("since"):
            bw.append("finished>=?"); ba.append(float(f["since"]))
        if f.get("until"):
            bw.append("finished<?"); ba.append(float(f["until"]))
        jobs = [j for j in (f.get("jobs") or []) if j]
        if jobs:
            bw.append("(" + " OR ".join("job LIKE ? ESCAPE '\\'" for _ in jobs) + ")")
            ba += [_glob(j) for j in jobs]
        builds_total = self.scalar("SELECT COUNT(*) FROM builds WHERE " + " AND ".join(bw), tuple(ba)) or 0
        return {
            "failed_builds": total,
            "builds_total": builds_total,
            "failed_pipelines": one(f"SELECT COUNT(DISTINCT job) {base}"),
            "analysed": one(f"SELECT COUNT(*) {base} AND status='done'"),
            "analysis_errors": one(f"SELECT COUNT(*) {base} AND status='error'"),
            "excluded": one(f"SELECT COUNT(*) {base} AND status='skipped'"),
            "in_progress": one(f"SELECT COUNT(*) {base} AND status IN ('queued','analysing')"),
            "from_deck": one(f"SELECT COUNT(*) {base} AND analysed_by LIKE 'Solutions deck%'"),
            "fixed_next_build": fixed_next,
            "pipelines_fixed_next_build": one(f"SELECT COUNT(DISTINCT job) {base} AND next_result='SUCCESS'"),
            "fixed_later": fixed_later,
            "still_failing": one(f"SELECT COUNT(*) {base} AND fixed_build IS NULL"),
            "rebuild_suggested": one(f"SELECT COUNT(*) {base} AND rebuild_can_fix=1"),
            "rebuilds_requested": rebuilds,
            "rebuilds_succeeded": rebuild_ok,
            "rebuilds_failed": rebuild_bad,
            "recurring": one(f"SELECT COUNT(*) FROM (SELECT fingerprint {base} AND fingerprint IS NOT NULL"
                             " GROUP BY fingerprint HAVING COUNT(*)>1)"),
            "avg_analysis_seconds": round(one(f"SELECT AVG(seconds) {base} AND seconds IS NOT NULL"), 1),
            "median_time_to_fix_seconds": round(fix_times[len(fix_times) // 2]) if fix_times else None,
            "by_category": by_cat,
            "bucket": bucket,
            "daily": daily,
            "top_signatures": top_sig,
            "top_pipelines": top_jobs,
        }
