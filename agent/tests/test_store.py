"""store.py: incident bookkeeping, rebuild tracking and pruning, on a throw-away SQLite file."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildforge_agent.store import Store  # noqa: E402


def incident(inc_id="BF-1", job="app/deploy", build=7, **extra):
    return {"id": inc_id, "job": job, "build": build, "status": "queued", **extra}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "sub" / "bf.db")

    def test_a_build_gets_only_one_incident(self):
        self.assertTrue(self.store.create_incident(incident()))
        self.assertFalse(self.store.create_incident(incident(inc_id="BF-2")))  # the plugin retried the same build
        self.assertEqual(self.store.incident_for("app/deploy", 7)["id"], "BF-1")

    def test_json_columns_round_trip_and_update_bumps_the_stamp(self):
        self.store.create_incident(incident(analysis={"category": "infrastructure"}))
        before = self.store.incident("BF-1")
        self.assertEqual(before["analysis"], {"category": "infrastructure"})
        time.sleep(0.01)
        self.store.update_incident("BF-1", status="done", analysis={"category": "test_failure"})
        after = self.store.incident("BF-1")
        self.assertEqual((after["status"], after["analysis"]["category"]), ("done", "test_failure"))
        self.assertGreater(after["updated"], before["updated"])

    def test_unknown_incident_is_none(self):
        self.assertIsNone(self.store.incident("BF-nope"))
        self.assertIsNone(self.store.incident_for("app/deploy", 1))

    def test_pending_lists_only_queued_and_analysing_in_creation_order(self):
        for n, status in enumerate(("done", "analysing", "queued"), start=1):
            self.store.create_incident(incident(inc_id=f"BF-{n}", build=n, status=status))
            time.sleep(0.01)
        self.assertEqual([i["id"] for i in self.store.pending()], ["BF-2", "BF-3"])

    def test_writes_bump_the_revision_the_dashboard_polls(self):
        rev = self.store.rev
        self.store.create_incident(incident())
        self.assertGreater(self.store.rev, rev)

    def test_rebuild_started_fills_the_oldest_open_request(self):
        self.store.create_incident(incident())
        first = self.store.add_rebuild("BF-1", "app/deploy", 7, "ann")
        second = self.store.add_rebuild("BF-1", "app/deploy", 7, "bob")
        self.store.rebuild_started("app/deploy", 7, 8)
        rows = {r["id"]: r for r in self.store.rebuilds_of("app/deploy", 7)}
        self.assertEqual((rows[first]["new_build"], rows[second]["new_build"]), (8, None))
        self.store.rebuild_finished("app/deploy", 8, "SUCCESS")
        self.assertEqual(self.store.rebuilds_of("app/deploy", 7)[0]["new_result"], "SUCCESS")

    def test_rebuild_started_without_a_request_records_one_linked_to_the_incident(self):
        self.store.create_incident(incident())
        self.store.rebuild_started("app/deploy", 7, 9)
        [row] = self.store.rebuilds_of("app/deploy", 7)
        self.assertEqual((row["incident_id"], row["new_build"], row["requested_by"]), ("BF-1", 9, "?"))

    def test_prune_drops_only_rows_older_than_the_cutoff(self):
        self.store.create_incident(incident(inc_id="BF-old", build=1))
        self.store._exec("UPDATE incidents SET created=100 WHERE id='BF-old'")  # noqa: SLF001
        self.store.create_incident(incident(inc_id="BF-new", build=2))
        self.store.record_build("app/deploy", 1, "FAILURE", None, 100)
        self.store.record_build("app/deploy", 2, "FAILURE", None, time.time())
        self.assertEqual(self.store.prune(time.time() - 3600), ["BF-old"])
        self.assertIsNone(self.store.incident("BF-old"))
        self.assertIsNotNone(self.store.incident("BF-new"))
        self.assertEqual(self.store.scalar("SELECT count(*) FROM builds"), 1)

    def test_old_databases_gain_the_columns_added_later(self):
        path = Path(self.tmp.name) / "old.db"
        Store(path)._db.close()  # noqa: SLF001
        db = sqlite3.connect(path)
        for col in ("analysed_at", "triggered_by", "commits"):  # what a 2.0.0 database looked like
            db.execute(f"ALTER TABLE incidents DROP COLUMN {col}")
        db.commit()
        db.close()
        store = Store(path)
        cols = {r[1] for r in store._db.execute("PRAGMA table_info(incidents)")}  # noqa: SLF001
        self.assertTrue({"analysed_at", "triggered_by", "commits"} <= cols)


if __name__ == "__main__":
    unittest.main()
