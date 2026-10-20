"""SQLite store: one incident per failed build, every completed build (for "fixed on the next build"), and rebuilds."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

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

    def list_incidents(self, limit: int = 50, offset: int = 0, q: str = "", category: str = "",
                       outcome: str = "", since: float = 0) -> tuple[int, list[dict]]:
        where, args = ["created>=?"], [since]
        if q:
            where.append("(id LIKE ? OR job LIKE ? OR signature LIKE ? OR stage LIKE ?)")
            args += [f"%{q}%"] * 4
        if category:
            where.append("category=?")
            args.append(category)
        if outcome == "fixed-next":
            where.append("next_result='SUCCESS'")
        elif outcome == "fixed-later":
            where.append("fixed_build IS NOT NULL AND (next_result IS NULL OR next_result<>'SUCCESS')")
        elif outcome == "open":
            where.append("fixed_build IS NULL")
        elif outcome == "rebuilt":
            where.append("EXISTS (SELECT 1 FROM rebuilds r WHERE r.incident_id=incidents.id)")
        sql_where = " WHERE " + " AND ".join(where)
        total = self.scalar("SELECT COUNT(*) FROM incidents" + sql_where, tuple(args)) or 0
        rows = self._rows("SELECT id, job, build, build_url, result, status, stage, signature, category,"
                          " rebuild_can_fix, analysis, error, seconds, next_build, next_result, next_is_rebuild,"
                          " fixed_build, fixed_at, failed_at, created FROM incidents" + sql_where +
                          " ORDER BY created DESC LIMIT ? OFFSET ?", (*args, limit, offset))
        return total, rows

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

    def rebuild_finished(self, job: str, new_build: int, result: str) -> None:
        self._exec("UPDATE rebuilds SET new_result=? WHERE job=? AND new_build=?", (result, job, new_build))

    def rebuilds_of(self, job: str, origin_build: int) -> list[dict]:
        return self._rows("SELECT * FROM rebuilds WHERE job=? AND origin_build=? ORDER BY id", (job, origin_build))

    # stats ----------------------------------------------------------------
    def stats(self, since: float) -> dict:
        def one(sql: str, *a):
            return self.scalar(sql, (since, *a)) or 0

        base = "FROM incidents WHERE created>=?"
        total = one(f"SELECT COUNT(*) {base}")
        fixed_next = one(f"SELECT COUNT(*) {base} AND next_result='SUCCESS'")
        fixed_later = one(f"SELECT COUNT(*) {base} AND fixed_build IS NOT NULL AND"
                          " (next_result IS NULL OR next_result<>'SUCCESS')")
        with self._lock:
            by_cat = {r[0] or "pending": r[1] for r in self._db.execute(
                f"SELECT category, COUNT(*) {base} GROUP BY category ORDER BY 2 DESC", (since,))}
            daily = [{"day": r[0], "failed": r[1], "fixed_next": r[2]} for r in self._db.execute(
                f"SELECT date(created,'unixepoch','localtime'), COUNT(*), SUM(next_result='SUCCESS') {base}"
                " GROUP BY 1 ORDER BY 1", (since,))]
            top_sig = [{"signature": r[0], "count": r[1], "pipelines": r[2], "last_seen": r[3], "fixed": r[4] or 0,
                        "last_id": r[5]} for r in self._db.execute(
                f"SELECT signature, COUNT(*), COUNT(DISTINCT job), MAX(created), SUM(fixed_build IS NOT NULL),"
                f" (SELECT i2.id FROM incidents i2 WHERE i2.signature=incidents.signature ORDER BY i2.created DESC LIMIT 1)"
                f" {base} AND signature IS NOT NULL GROUP BY signature ORDER BY 2 DESC, 4 DESC LIMIT 10", (since,))]
            top_jobs = [{"job": r[0], "failed": r[1], "fixed_next": r[2] or 0, "open": r[3] or 0} for r in
                        self._db.execute(f"SELECT job, COUNT(*), SUM(next_result='SUCCESS'), SUM(fixed_build IS NULL)"
                                         f" {base} GROUP BY job ORDER BY 2 DESC LIMIT 10", (since,))]
            fix_times = sorted(r[0] for r in self._db.execute(
                f"SELECT fixed_at - failed_at {base} AND fixed_at IS NOT NULL AND failed_at IS NOT NULL", (since,)))
        rebuilds = self.scalar("SELECT COUNT(*) FROM rebuilds WHERE requested_at>=?", (since,)) or 0
        rebuild_ok = self.scalar("SELECT COUNT(*) FROM rebuilds WHERE requested_at>=? AND new_result='SUCCESS'",
                                 (since,)) or 0
        rebuild_bad = self.scalar("SELECT COUNT(*) FROM rebuilds WHERE requested_at>=? AND new_result IS NOT NULL"
                                  " AND new_result<>'SUCCESS'", (since,)) or 0
        return {
            "failed_builds": total,
            "failed_pipelines": one(f"SELECT COUNT(DISTINCT job) {base}"),
            "analysed": one(f"SELECT COUNT(*) {base} AND status='done'"),
            "analysis_errors": one(f"SELECT COUNT(*) {base} AND status='error'"),
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
            "avg_analysis_seconds": round(self.scalar(f"SELECT AVG(seconds) {base} AND seconds IS NOT NULL",
                                                      (since,)) or 0, 1),
            "median_time_to_fix_seconds": round(fix_times[len(fix_times) // 2]) if fix_times else None,
            "by_category": by_cat,
            "daily": daily,
            "top_signatures": top_sig,
            "top_pipelines": top_jobs,
        }
