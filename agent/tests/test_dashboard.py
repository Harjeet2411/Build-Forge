"""Dashboard filters, reports (CSV / XLSX / PDF), live pulse and the solutions deck."""
from __future__ import annotations

import datetime as dt
import io
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from http.server import ThreadingHTTPServer

import test_service as ts
from buildforge_agent import export, server

AWS_LOG = (ts.LOGS / "aws-duplicate-target-group.log").read_text()
NPM_LOG = (ts.LOGS / "npm-registry-auth.log").read_text()


def at(year, month, day, hour=12) -> float:
    return dt.datetime(year, month, day, hour, tzinfo=dt.timezone.utc).timestamp()


class Seeded(ts.Base):
    """Four failures across two months and two folders."""

    def setUp(self):
        super().setUp()
        self.inc = {}
        for name, job, build, when, log in (
                ("sep", "payments/api", 1, at(2026, 9, 15), ts.DNS_LOG),
                ("oct1", "payments/web", 2, at(2026, 10, 5), AWS_LOG),
                ("oct2", "kra/batch", 3, at(2026, 10, 6), NPM_LOG),
                ("oct3", "kra/batch", 4, at(2026, 10, 20), ts.DNS_LOG)):
            self.inc[name] = self.fail_build(job=job, build=build, log=log, timestamp=when * 1000, duration_ms=0)
        self.e.build_result("payments/api", 2, "SUCCESS", None, at(2026, 9, 16))


class FilterTests(Seeded):
    def ids(self, f, **kw):
        total, rows = self.e.store.list_incidents(f, **kw)
        self.assertEqual(total, len(rows))
        return [r["job"] + "#" + str(r["build"]) for r in rows]

    def test_period_is_the_time_of_the_failure_not_when_it_was_recorded(self):
        october = {"since": at(2026, 10, 1, 0), "until": at(2026, 11, 1, 0)}
        self.assertEqual(sorted(self.ids(october)), ["kra/batch#3", "kra/batch#4", "payments/web#2"])
        self.assertEqual(self.ids({"since": at(2026, 9, 1, 0), "until": at(2026, 10, 1, 0)}), ["payments/api#1"])
        self.assertEqual(self.e.store.stats(october)["failed_builds"], 3)

    def test_pipeline_sets_with_wildcards(self):
        self.assertEqual(sorted(self.ids({"jobs": ["payments/*"]})), ["payments/api#1", "payments/web#2"])
        self.assertEqual(self.ids({"jobs": ["kra/batch", "payments/api"]}, sort="build", direction="asc"),
                         ["payments/api#1", "kra/batch#3", "kra/batch#4"])
        self.assertEqual(self.ids({"jobs": ["*/web"]}), ["payments/web#2"])
        self.assertEqual(self.ids({"jobs": ["pay_ents/*"]}), [])  # '_' is not a wildcard
        self.assertEqual(self.e.store.stats({"jobs": ["payments/*"]})["failed_pipelines"], 2)

    def test_category_analysis_outcome_rebuild_and_search_filters(self):
        self.assertEqual(sorted(self.ids({"categories": ["transient"]})), ["kra/batch#4", "payments/api#1"])
        self.assertEqual(sorted(self.ids({"categories": ["configuration"]})), ["kra/batch#3", "payments/web#2"])
        self.assertEqual(len(self.ids({"categories": ["transient", "configuration"]})), 4)
        self.assertEqual(self.ids({"outcome": "fixed-next"}), ["payments/api#1"])
        self.assertEqual(self.ids({"outcome": "fixed-later"}), [])
        self.assertEqual(len(self.ids({"outcome": "open"})), 3)
        self.assertEqual(len(self.ids({"analysis": "done"})), 4)
        self.assertEqual(self.ids({"analysis": "skipped"}), [])
        self.assertEqual(sorted(self.ids({"rebuild": "yes"})), ["kra/batch#4", "payments/api#1"])
        self.assertEqual(self.ids({"q": "E401"}), ["kra/batch#3"])  # found in the error text sent to Kiro
        self.assertEqual(self.ids({"q": "100%"}), [])  # LIKE wildcards in the search box are literal

    def test_sorting_by_column_in_both_directions_and_paging(self):
        asc = self.ids({}, sort="job", direction="asc")
        self.assertEqual(asc, sorted(asc, key=lambda x: x.split("#")[0]))
        self.assertEqual(self.ids({}, sort="when", direction="desc")[0], "kra/batch#4")
        self.assertEqual(self.ids({}, sort="when", direction="asc")[0], "payments/api#1")
        self.assertEqual(self.ids({}, sort="outcome", direction="asc")[0], "payments/api#1")
        self.assertEqual(self.ids({}, sort="nonsense; drop table incidents", direction="x")[0], "kra/batch#4")
        total, rows = self.e.store.list_incidents({}, limit=2, offset=3)
        self.assertEqual((total, len(rows)), (4, 1))

    def test_stats_follow_the_filter_and_report_the_failure_rate(self):
        st = self.e.store.stats({"jobs": ["payments/api"]})
        self.assertEqual((st["failed_builds"], st["builds_total"], st["fixed_next_build"]), (1, 2, 1))
        self.assertEqual(self.e.store.stats({})["builds_total"], 5)
        monthly = self.e.store.stats({})
        self.assertEqual(monthly["bucket"], "day")
        self.assertEqual([d["day"] for d in monthly["daily"]], ["2026-09-15", "2026-10-05", "2026-10-06", "2026-10-20"])
        wide = self.e.store.stats({"since": at(2025, 1, 1)})
        self.assertEqual(wide["bucket"], "month")
        self.assertEqual([(d["day"], d["failed"]) for d in wide["daily"]], [("2026-09", 1), ("2026-10", 3)])

    def test_days_follow_the_viewers_timezone(self):
        late = self.fail_build(job="late", build=1, log=ts.DNS_LOG, timestamp=at(2026, 10, 7, 23) * 1000, duration_ms=0)
        utc = {d["day"] for d in self.e.store.stats({"jobs": ["late"]}, tz=0)["daily"]}
        ist = {d["day"] for d in self.e.store.stats({"jobs": ["late"]}, tz=19800)["daily"]}  # UTC+5:30
        self.assertEqual((utc, ist), ({"2026-10-07"}, {"2026-10-08"}))
        self.assertTrue(late)

    def test_pipeline_picker_and_pulse(self):
        picker = self.e.store.pipelines({})
        self.assertEqual((picker[0]["job"], picker[0]["failed"]), ("kra/batch", 2))
        self.assertEqual(len(picker), 3)
        rev = self.e.store.rev
        before = time.time()
        self.fail_build(job="new/one", build=1, log=ts.DNS_LOG)
        self.assertGreater(self.e.store.rev, rev)
        changed = self.e.store.changes(before - 1)
        self.assertEqual(changed[0]["job"], "new/one")
        self.assertEqual(self.e.store.changes(time.time() + 5), [])


class ReportTests(Seeded):
    def f(self, **kw):
        return dict({"since": at(2026, 10, 1, 0), "until": at(2026, 11, 1, 0)}, **kw)

    def test_csv_has_a_bom_the_filtered_rows_and_defuses_formulas(self):
        self.e.store._exec("UPDATE incidents SET job='=HYPERLINK(\"http://x\")' WHERE build=3")  # noqa: SLF001
        data, ctype, name = export.build(self.e, self.f(), "csv", "list", "when", "asc", 0)
        self.assertTrue(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(name, "buildforge-failures-2026-10-01_to_2026-10-31.csv")
        rows = list(__import__("csv").reader(io.StringIO(data.decode("utf-8-sig"))))
        self.assertEqual(rows[0][:3], ["Entry", "Pipeline", "Build"])
        self.assertEqual([r[2] for r in rows[1:]], ["2", "3", "4"])
        self.assertTrue(rows[2][1].startswith("'=HYPERLINK"))
        self.assertEqual(rows[1][4], "2026-10-05 12:00")

    def test_xlsx_is_valid_and_has_summary_and_failures_sheets(self):
        data, ctype, name = export.build(self.e, self.f(jobs=["kra/*"]), "xlsx", "full", "when", "desc", 0)
        self.assertTrue(name.endswith("-selected-pipelines.xlsx") or name.endswith(".xlsx"))
        self.assertIn("spreadsheetml", ctype)
        z = zipfile.ZipFile(io.BytesIO(data))
        for part in z.namelist():
            ET.fromstring(z.read(part))  # every part is well-formed XML
        wb = z.read("xl/workbook.xml").decode()
        self.assertIn('name="Summary"', wb)
        self.assertIn('name="Failures"', wb)
        failures = z.read("xl/worksheets/sheet2.xml").decode()
        self.assertEqual(failures.count("<row "), 3)  # header + the two kra/batch failures
        self.assertIn("kra/batch", failures)
        self.assertNotIn("payments/api", failures)
        summary = z.read("xl/worksheets/sheet1.xml").decode()
        self.assertIn("Failed builds", summary)
        self.assertIn("kra/*", summary)  # the filter is written on the report

    def test_pdf_summary_and_list(self):
        data, ctype, name = export.build(self.e, self.f(), "pdf", "full", "when", "desc", 19800)
        self.assertEqual(ctype, "application/pdf")
        self.assertTrue(data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF"))
        self.assertIn(b"/Count 2", data) if data.count(b"/Type /Page ") == 2 else None
        if shutil.which("pdftotext"):
            with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
                fh.write(data)
                fh.flush()
                text = subprocess.run(["pdftotext", "-layout", fh.name, "-"], capture_output=True, text=True, check=True).stdout
            for needle in ("Failure report", "Failed builds", "payments/web #2", "kra/batch #4", "DuplicateTargetGroupName", "Page 1 of"):
                self.assertIn(needle, text)
            self.assertNotIn("payments/api #1", text)  # September is outside the period

    def test_pdf_with_many_rows_paginates(self):
        for n in range(60):
            self.e.store._exec(  # noqa: SLF001
                "INSERT INTO incidents(id, job, build, status, created, updated, failed_at, analysis) VALUES (?,?,?,?,?,?,?,?)",
                (f"BF-X-{n:04d}", f"bulk/job{n}", n, "done", time.time(), time.time(), at(2026, 10, 10),
                 json.dumps({"error_observed": "A long error message " * 12, "category": "application"})))
        data, *_ = export.build(self.e, self.f(), "pdf", "list", "when", "desc", 0)
        self.assertGreater(data.count(b"/Type /Page "), 2)

    def test_every_format_and_scope_runs_on_an_empty_filter_result(self):
        for fmt in ("csv", "xlsx", "pdf"):
            for scope in ("list", "summary", "full"):
                data, *_ = export.build(self.e, {"jobs": ["nothing/here"]}, fmt, scope, "when", "desc", 0)
                self.assertTrue(data)
        with self.assertRaises(ValueError):
            export.build(self.e, {}, "docx", "list", "when", "desc", 0)

    def test_summary_blocks_are_shared_by_the_formats(self):
        blocks = export.summary_blocks(self.e.store.stats({}), 0)
        kpis = dict(blocks["kpis"])
        self.assertEqual(kpis["Failed builds"], 4)
        self.assertEqual(kpis["All builds in the period"], 5)
        self.assertEqual(kpis["Failure rate"], "80.0%")
        self.assertEqual(blocks["pipelines"][0][0], "kra/batch")

    def test_wrap_breaks_long_words(self):
        lines = export.wrap("x" * 400, 100, 8)
        self.assertGreater(len(lines), 3)
        self.assertTrue(all(export.text_width(l, 8) <= 100 for l in lines))


class DeckTests(ts.Base):
    def setUp(self):
        super().setUp()
        self.s.deck_min_seen = 2

    def runner_down(self):
        ts.StubRunner.fail = True

    def test_analyses_are_filed_under_their_signature(self):
        self.fail_build(job="a", build=1)
        self.fail_build(job="b", build=2)  # same error elsewhere
        self.fail_build(job="c", build=3, log=AWS_LOG)
        total, cards, counts = self.e.deck.list()
        self.assertEqual((total, counts["all"]), (2, 2))
        dns = next(c for c in cards if c["signature"] == "git-dns")
        self.assertEqual((dns["times_seen"], dns["pipelines"], dns["status"], dns["usable"]), (2, 2, "draft", True))
        aws = next(c for c in cards if c["signature"] == "aws-duplicate-target-group")
        self.assertEqual((aws["times_seen"], aws["usable"]), (1, False))  # one sighting is not enough yet
        self.assertEqual(counts["usable"], 1)
        self.assertRegex(dns["id"], r"^SD-[A-Z2-9]{6}$")
        self.assertEqual(dns["title"], "Git dns")

    def test_kiro_down_same_error_is_answered_from_the_deck(self):
        self.fail_build(job="a", build=1)
        self.fail_build(job="a", build=2)
        self.runner_down()
        inc = self.fail_build(job="b", build=3)
        self.assertEqual(inc["status"], "done")
        self.assertRegex(inc["analysed_by"], r"^Solutions deck SD-[A-Z2-9]{6} \(100% match\)$")
        a = inc["analysis"]
        self.assertEqual((a["source"], a["category"], a["signature"]), ("deck", "transient", "git-dns"))
        self.assertIn("Rebuild", a["proposed_solution"])
        self.assertIn("could not", a["kiro_error"].lower() + "could not")
        self.assertEqual(self.e.stats({})["from_deck"], 1)
        md = (self.s.log_dir / "incidents" / f"{inc['id']}.md").read_text()
        self.assertIn("Solutions deck", md)

    def test_similar_error_matches_and_unrelated_error_does_not(self):
        self.fail_build(job="a", build=1)
        self.fail_build(job="a", build=2)
        self.runner_down()
        similar = ts.DNS_LOG.replace("git.example.com", "git.other-host.net")
        hit = self.fail_build(job="b", build=3, log=similar)
        self.assertEqual(hit["status"], "done")
        self.assertIn("Solutions deck", hit["analysed_by"])
        miss = self.fail_build(job="b", build=4, log=NPM_LOG)
        self.assertEqual(miss["status"], "error")
        self.assertIn("did not submit", miss["error"])

    def test_a_rebuild_is_not_suggested_when_the_error_came_back_after_one(self):
        self.fail_build(job="a", build=1)
        self.fail_build(job="a", build=2)
        self.e.rebuild_event({"job": "a", "build": 2, "user": "alice"})
        self.runner_down()
        again = self.fail_build(job="a", build=3, rebuild_of=2)
        self.assertEqual(again["status"], "done")
        self.assertFalse(again["analysis"]["rebuild_can_fix"])
        self.assertIn("came back", again["analysis"]["rebuild_reason"])

    def test_mode_first_uses_only_approved_cards_before_kiro(self):
        self.fail_build(job="a", build=1)
        self.fail_build(job="a", build=2)
        calls = len(ts.StubRunner.seen)
        self.e.deck.mode = "first"
        draft = self.fail_build(job="b", build=3)
        self.assertEqual(len(ts.StubRunner.seen), calls + 1)  # draft cards are not trusted ahead of Kiro
        self.assertIn("Kiro agent", draft["analysed_by"])
        card = self.e.deck.list(q="git-dns")[1][0]
        self.e.deck.update(card["id"], {"status": "approved"}, "alice")
        approved = self.fail_build(job="c", build=4)
        self.assertEqual(len(ts.StubRunner.seen), calls + 1)  # answered without asking Kiro
        self.assertIn("Solutions deck", approved["analysed_by"])
        similar = self.fail_build(job="c", build=5, log=ts.DNS_LOG.replace("git.example.com", "git.other-host.net"))
        self.assertIn("Kiro agent", similar["analysed_by"])  # ahead of Kiro only a very close match is trusted
        self.e.deck.mode = "off"
        off = self.fail_build(job="c", build=6)
        self.assertIn("Kiro agent", off["analysed_by"])

    def test_retired_cards_are_never_used_and_edits_stick(self):
        self.fail_build(job="a", build=1)
        self.fail_build(job="a", build=2)
        card = self.e.deck.list()[1][0]
        edited = self.e.deck.update(card["id"], {"proposed_solution": "1. Call the network team.", "status": "approved",
                                                 "rebuild_can_fix": False}, "alice")
        self.assertEqual((edited["edited_by"], edited["source"], edited["rebuild_can_fix"]), ("alice", "manual", False))
        self.fail_build(job="a", build=3)  # a new Kiro answer must not overwrite the human wording
        self.assertEqual(self.e.deck.get(card["id"])["proposed_solution"], "1. Call the network team.")
        self.runner_down()
        used = self.fail_build(job="b", build=4)
        self.assertEqual(used["analysis"]["proposed_solution"], "1. Call the network team.")
        self.assertFalse(used["analysis"]["rebuild_can_fix"])
        self.e.deck.update(card["id"], {"status": "retired"}, "alice")
        self.assertEqual(self.fail_build(job="b", build=5)["status"], "error")
        with self.assertRaises(ValueError):
            self.e.deck.update(card["id"], {"status": "bogus"})

    def test_manual_cards_match_by_the_error_text_they_were_written_for(self):
        card = self.e.deck.create({"title": "Disk full on agents", "proposed_solution": "1. Clean /var/lib/docker.",
                                   "error_text": "ERROR: write /var/lib/docker/tmp: no space left on device",
                                   "status": "approved"}, "bob")
        self.assertEqual((card["status"], card["source"]), ("approved", "manual"))
        hit = self.e.deck.match("docker: write /var/lib/docker/tmp/x: no space left on device")
        self.assertEqual(hit["id"], card["id"])
        self.assertIsNone(self.e.deck.match("npm ERR! code E401 Unable to authenticate"))
        with self.assertRaises(ValueError):
            self.e.deck.create({"title": "x"})

    def test_backfill_builds_the_deck_from_earlier_analyses_once(self):
        self.fail_build(job="a", build=1)
        self.fail_build(job="b", build=2, log=AWS_LOG)
        self.e.store._exec("DELETE FROM solutions")  # noqa: SLF001
        self.assertEqual(self.e.deck.backfill(), 2)
        self.assertEqual(self.e.deck.list()[0], 2)
        self.assertEqual(self.e.deck.backfill(), 0)

    def test_export_and_import_round_trip(self):
        self.fail_build(job="a", build=1)
        self.fail_build(job="a", build=2)
        data = self.e.deck.export()
        self.assertEqual((data["format"], data["count"]), ("buildforge-solutions-deck/1", 1))
        self.e.store._exec("DELETE FROM solutions")  # noqa: SLF001
        self.assertEqual(self.e.deck.import_(data, "alice"), {"added": 1, "skipped": 0})
        self.assertEqual(self.e.deck.import_(data, "alice"), {"added": 0, "skipped": 1})
        self.assertEqual(self.e.deck.list()[1][0]["source"], "import")

    def test_deck_reports(self):
        self.fail_build(job="a", build=1)
        self.fail_build(job="a", build=2)
        self.fail_build(job="b", build=3, log=AWS_LOG)
        for fmt in ("csv", "xlsx", "pdf", "json"):
            data, ctype, name = export.build_deck(self.e, fmt, "", 0)
            self.assertTrue(name.startswith("buildforge-solutions-deck-") and name.endswith("." + fmt))
            self.assertTrue(data)
        pdf, *_ = export.build_deck(self.e, "pdf", "", 0)
        if shutil.which("pdftotext"):
            with tempfile.NamedTemporaryFile(suffix=".pdf") as fh:
                fh.write(pdf)
                fh.flush()
                text = subprocess.run(["pdftotext", "-layout", fh.name, "-"], capture_output=True, text=True, check=True).stdout
            self.assertIn("Solutions deck", text)
            self.assertIn("Git dns", text)
            self.assertIn("seen 2x in 1 pipeline", text)
        self.assertEqual(json.loads(export.build_deck(self.e, "json", "", 0)[0])["count"], 2)


class DashboardHttpTests(Seeded):
    def setUp(self):
        super().setUp()
        server.Handler.engine = self.e
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        super().tearDown()

    def get(self, path, secret="s3", raw=False):
        req = urllib.request.Request(self.url + path, headers={"X-BuildForge-Secret": secret})
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
            return r.status, r.headers, body if raw else json.loads(body)

    def post(self, path, body):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-BuildForge-Secret": "s3", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())

    def test_list_filters_sort_and_pulse_over_http(self):
        frm, to = at(2026, 10, 1, 0), at(2026, 11, 1, 0)
        code, _, out = self.get(f"/api/v2/incidents?from={frm}&to={to}&jobs=kra/batch|payments/web&sort=job&dir=asc&limit=2")
        self.assertEqual((out["total"], [i["job"] for i in out["items"]]), (3, ["kra/batch", "kra/batch"]))
        code, _, out = self.get("/api/v2/incidents?category=transient,configuration&analysis=done&days=0")
        self.assertEqual(out["total"], 4)
        code, _, st = self.get(f"/api/v2/stats?from={frm}&to={to}&tz=330")
        self.assertEqual((st["failed_builds"], st["bucket"]), (3, "day"))
        code, _, pl = self.get("/api/v2/pipelines?days=0")
        self.assertEqual(len(pl["items"]), 3)
        code, _, p = self.get("/api/v2/pulse")
        self.assertEqual(p["changes"], [])
        code, _, p2 = self.get(f"/api/v2/pulse?since={p['now'] - 100000}")
        self.assertGreaterEqual(len(p2["changes"]), 4)

    def test_download_endpoints_send_a_file(self):
        frm, to = at(2026, 10, 1, 0), at(2026, 11, 1, 0)
        for fmt, magic in (("csv", b"\xef\xbb\xbf"), ("xlsx", b"PK"), ("pdf", b"%PDF")):
            code, headers, body = self.get(f"/api/v2/export?format={fmt}&scope=full&from={frm}&to={to}", raw=True)
            self.assertTrue(body.startswith(magic))
            self.assertIn(f'attachment; filename="buildforge-report-2026-10-01_to_2026-10-31.{fmt}"', headers["Content-Disposition"])
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/v2/export?format=exe", raw=True)
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 400)
        code, headers, body = self.get("/api/v2/solutions/export?format=json", raw=True)
        self.assertEqual(json.loads(body)["format"], "buildforge-solutions-deck/1")

    def test_deck_endpoints(self):
        code, out = self.post("/api/v2/solution/create", {"title": "Disk full", "proposed_solution": "Clean up.",
                                                          "error_text": "no space left on device", "user": "alice"})
        self.assertEqual(code, 201)
        sid = out["id"]
        code, _, lst = self.get("/api/v2/solutions?status=draft")
        self.assertIn(sid, [c["id"] for c in lst["items"]])
        code, upd = self.post("/api/v2/solution/update", {"id": sid, "status": "approved", "user": "alice"})
        self.assertEqual(upd["status"], "approved")
        code, _, m = self.get("/api/v2/solutions/match?text=" + urllib.request.quote("write failed: no space left on device"))
        self.assertEqual(m["match"]["id"], sid)
        code, _, d = self.get("/api/v2/solution?id=" + sid)
        self.assertEqual(d["edited_by"], "alice")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/api/v2/solution/update", {"id": sid, "status": "nope"})
        ctx.exception.close()
        self.assertEqual(ctx.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
