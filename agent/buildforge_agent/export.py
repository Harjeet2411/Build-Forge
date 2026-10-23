"""Reports for the dashboard: CSV, XLSX and PDF, written with the standard library only.

The same filter that drives the dashboard drives the report, so what an admin sees is what they download:
  scope "list"    the failed builds, one row each
  scope "summary" the numbers on the dashboard (cards, per day, categories, top errors, top pipelines)
  scope "full"    both
and the solutions deck as its own report (CSV, XLSX, PDF or JSON).
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import time
import zipfile
from xml.sax.saxutils import escape as xml_escape

from .incidents import outcome_text

FORMATS = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "json": "application/json; charset=utf-8",
}
MAX_ROWS = 50000
MAX_PDF_ROWS = 3000
CATEGORY_LABEL = {"application": "Application", "infrastructure": "Infrastructure", "configuration": "Configuration",
                  "dependency": "Dependency", "test_failure": "Test failure", "transient": "Transient",
                  "unknown": "Unknown", "excluded": "Excluded from analysis", "not_analysed": "Not analysed",
                  "pending": "Analysing"}
ANALYSIS_LABEL = {"done": "Analysed", "error": "Not analysed (error)", "skipped": "Excluded from analysis",
                  "queued": "Queued", "analysing": "Analysing"}


# ---------------------------------------------------------------- shared helpers
def _tz(seconds: int) -> dt.timezone:
    return dt.timezone(dt.timedelta(seconds=seconds))


def fmt_time(ts: float | None, tz: int, with_time: bool = True) -> str:
    if not ts:
        return ""
    return dt.datetime.fromtimestamp(ts, _tz(tz)).strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def fmt_dur(seconds: float | None) -> str:
    if seconds is None:
        return ""
    s = int(seconds)
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


def plain(text: str | None) -> str:
    """Markdown-ish text -> plain text for cells and PDF."""
    t = re.sub(r"```[a-zA-Z0-9_-]*\n?", "", str(text or ""))
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    return t.strip()


def describe_filters(f: dict, tz: int) -> list[tuple[str, str]]:
    out = []
    if f.get("since") or f.get("until"):
        a = fmt_time(f.get("since"), tz, False) or "the beginning"
        b = fmt_time(f["until"] - 1, tz, False) if f.get("until") else "now"
        out.append(("Period", f"{a} to {b}"))
    else:
        out.append(("Period", "All time"))
    if f.get("jobs"):
        out.append(("Pipelines", ", ".join(f["jobs"])))
    if f.get("q"):
        out.append(("Search", f["q"]))
    if f.get("categories"):
        out.append(("Category", ", ".join(CATEGORY_LABEL.get(c, c) for c in f["categories"])))
    if f.get("outcome"):
        out.append(("Outcome", {"fixed-next": "Fixed on the next build", "fixed-later": "Fixed later",
                                "open": "Still failing", "fixed": "Fixed", "rebuilt": "Rebuilt via Build Forge"}
                    .get(f["outcome"], f["outcome"])))
    if f.get("analysis"):
        out.append(("Analysis", {"done": "Analysed", "error": "Not analysed (error)", "skipped": "Excluded from analysis",
                                 "progress": "In progress"}.get(f["analysis"], f["analysis"])))
    if f.get("rebuild"):
        out.append(("Rebuild suggested", f["rebuild"]))
    if f.get("result"):
        out.append(("Result", f["result"]))
    return out


def file_stem(f: dict, tz: int, what: str) -> str:
    if f.get("since") or f.get("until"):
        a = fmt_time(f.get("since"), tz, False) or "start"
        b = fmt_time(f["until"] - 1, tz, False) if f.get("until") else dt.date.today().isoformat()
        period = f"{a}_to_{b}"
    else:
        period = "all-time"
    extra = "-selected-pipelines" if f.get("jobs") else ""
    return f"buildforge-{what}-{period}{extra}"


# ---------------------------------------------------------------- the failure rows
COLUMNS = [  # key, header, xlsx width, in the PDF table
    ("id", "Entry", 20, True), ("job", "Pipeline", 34, True), ("build", "Build", 8, True), ("result", "Result", 11, False),
    ("when", "Failed at", 17, True), ("stage", "Failed stage", 20, True), ("node", "Node", 22, False),
    ("analysis", "Analysis", 20, False), ("analysed_by", "Analysed by", 26, False), ("category", "Category", 15, True),
    ("severity", "Severity", 10, False), ("confidence", "Confidence", 11, False), ("signature", "Error signature", 28, False),
    ("error_observed", "Error observed", 60, True), ("proposed_solution", "Proposed solution", 80, False),
    ("rebuild", "Rebuild can fix", 14, False), ("rebuild_reason", "Rebuild reason", 36, False),
    ("outcome", "Outcome", 34, True), ("next_build", "Next build", 11, False), ("next_result", "Next result", 12, False),
    ("fixed_build", "Fixed in build", 14, False), ("time_to_fix", "Time to fix", 13, False), ("url", "Build URL", 50, False),
]


def failure_row(i: dict, tz: int) -> dict:
    a = i.get("analysis") or {}
    done = i["status"] == "done"
    return {
        "id": i["id"], "job": i["job"], "build": i["build"], "result": i.get("result") or "",
        "when": i.get("failed_at") or i.get("created"), "stage": i.get("stage") or "", "node": i.get("node") or "",
        "analysis": ANALYSIS_LABEL.get(i["status"], i["status"]), "analysed_by": i.get("analysed_by") or "",
        "category": CATEGORY_LABEL.get(a.get("category"), a.get("category") or "") if done else
                    CATEGORY_LABEL.get({"skipped": "excluded", "error": "not_analysed"}.get(i["status"], "pending"), ""),
        "severity": a.get("severity") or "",
        "confidence": float(a["confidence"]) if a.get("confidence") is not None else None,
        "signature": a.get("signature") or i.get("signature") or "",
        "error_observed": plain(a.get("error_observed") or i.get("error")),
        "proposed_solution": plain(a.get("proposed_solution")),
        "rebuild": ("yes" if a.get("rebuild_can_fix") else "no") if done else "",
        "rebuild_reason": plain(a.get("rebuild_reason")), "outcome": outcome_text(i),
        "next_build": i.get("next_build") or "", "next_result": i.get("next_result") or "",
        "fixed_build": i.get("fixed_build") or "",
        "time_to_fix": fmt_dur(i["fixed_at"] - i["failed_at"]) if i.get("fixed_at") and i.get("failed_at") else "",
        "url": i.get("build_url") or "",
    }


def summary_blocks(s: dict, tz: int) -> dict:
    """The dashboard numbers as plain lists, shared by every format."""
    total = s["failed_builds"] or 0

    def pct(n):
        return f"{round(100 * n / total)}%" if total else "-"

    rate = (total / s["builds_total"]) if s.get("builds_total") else None
    kpis = [
        ("Failed builds", total), ("Pipelines with failures", s["failed_pipelines"]),
        ("All builds in the period", s.get("builds_total") or 0),
        ("Failure rate", f"{rate * 100:.1f}%" if rate is not None else "-"),
        ("Fixed on the next build", f"{s['fixed_next_build']} ({pct(s['fixed_next_build'])})"),
        ("Pipelines fixed on the next build", s["pipelines_fixed_next_build"]),
        ("Fixed later", f"{s['fixed_later']} ({pct(s['fixed_later'])})"),
        ("Still failing", s["still_failing"]),
        ("Analysed", s["analysed"]), ("Answered from the solutions deck", s.get("from_deck", 0)),
        ("Excluded from analysis", s.get("excluded", 0)), ("Not analysed (error)", s["analysis_errors"]),
        ("Rebuild suggested", s["rebuild_suggested"]), ("Rebuilds via Build Forge", s["rebuilds_requested"]),
        ("Rebuilds that went green", s["rebuilds_succeeded"]), ("Rebuilds that failed again", s["rebuilds_failed"]),
        ("Recurring errors", s["recurring"]), ("Average analysis time", fmt_dur(s["avg_analysis_seconds"]) or "-"),
        ("Median time to fix", fmt_dur(s["median_time_to_fix_seconds"]) or "-"),
    ]
    return {
        "kpis": kpis,
        "per": [(d["day"], d["failed"], d["fixed_next"]) for d in s["daily"]],
        "per_label": "Month" if s["bucket"] == "month" else "Day",
        "categories": [(CATEGORY_LABEL.get(k, k), v) for k, v in s["by_category"].items()],
        "errors": [(e["signature"], e["count"], e["pipelines"], e["fixed"], fmt_time(e["last_seen"], tz))
                   for e in s["top_signatures"]],
        "pipelines": [(p["job"], p["failed"], p["fixed_next"], p["open"]) for p in s["top_pipelines"]],
    }


# ---------------------------------------------------------------- CSV
def _csv_safe(v):
    """Excel runs a cell that starts with = + - @ as a formula; error text comes from build logs, so defuse it."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def to_csv(title: str, filters: list, rows: list[dict] | None, blocks: dict | None, tz: int) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    if blocks is not None:
        w.writerow([title])
        for k, v in filters:
            w.writerow([k, _csv_safe(v)])
        w.writerow(["Generated", fmt_time(time.time(), tz)])
        w.writerow([])
        w.writerow(["Summary"])
        for k, v in blocks["kpis"]:
            w.writerow([k, v])
        for name, header, data in (
            ("By category", ["Category", "Failures"], blocks["categories"]),
            (f"Per {blocks['per_label'].lower()}", [blocks["per_label"], "Failed", "Fixed on the next build"], blocks["per"]),
            ("Most frequent errors", ["Signature", "Times", "Pipelines", "Fixed", "Last seen"], blocks["errors"]),
            ("Pipelines that fail most", ["Pipeline", "Failed", "Fixed on the next build", "Still failing"], blocks["pipelines"]),
        ):
            w.writerow([])
            w.writerow([name])
            w.writerow(header)
            for r in data:
                w.writerow([_csv_safe(c) for c in r])
        if rows is not None:
            w.writerow([])
            w.writerow(["Failed builds"])
    if rows is not None:
        w.writerow([c[1] for c in COLUMNS])
        for r in rows:
            w.writerow([_csv_safe(fmt_time(r[k], tz) if k == "when" else
                                  (f"{round(r[k] * 100)}%" if k == "confidence" and r[k] is not None else r[k]))
                        if r[k] is not None else "" for k, *_ in COLUMNS])
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")  # BOM: Excel opens it as UTF-8


# ---------------------------------------------------------------- XLSX
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _col(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class Sheet:
    """One worksheet: rows of (value, style). Styles: 0 plain, 1 header, 2 wrapped, 3 title, 4 bold, 5 date, 6 percent."""

    def __init__(self, name: str, widths: list[float], freeze_header: bool = False, autofilter: bool = False):
        self.name, self.widths, self.rows = name[:31], widths, []
        self.freeze, self.filter = freeze_header, autofilter

    def add(self, values: list, style: int | list = 0) -> None:
        self.rows.append([(v, style[i] if isinstance(style, list) else style) for i, v in enumerate(values)])

    def xml(self) -> str:
        ncols = max([len(r) for r in self.rows] + [1])
        parts = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
        parts.append('<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
                     + ('<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>' if self.freeze else "")
                     + "</sheetView></sheetViews>")
        parts.append("<cols>" + "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>'
                                        for i, w in enumerate(self.widths)) + "</cols>")
        parts.append("<sheetData>")
        for n, row in enumerate(self.rows, 1):
            cells = []
            for c, (v, st) in enumerate(row):
                ref = f"{_col(c)}{n}"
                if v is None or v == "":
                    if st:
                        cells.append(f'<c r="{ref}" s="{st}"/>')
                elif isinstance(v, bool):
                    cells.append(f'<c r="{ref}" s="{st}" t="inlineStr"><is><t>{"yes" if v else "no"}</t></is></c>')
                elif isinstance(v, (int, float)):
                    cells.append(f'<c r="{ref}" s="{st}"><v>{v}</v></c>')
                else:
                    text = xml_escape(_CTRL.sub("", str(v))[:32000])
                    cells.append(f'<c r="{ref}" s="{st}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>')
            parts.append(f'<row r="{n}">{"".join(cells)}</row>')
        parts.append("</sheetData>")
        if self.filter and self.rows:
            parts.append(f'<autoFilter ref="A1:{_col(ncols - 1)}{len(self.rows)}"/>')
        parts.append('<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>'
                     '<pageSetup orientation="landscape" fitToHeight="0"/></worksheet>')
        return "".join(parts)


STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd\\ hh:mm"/></numFmts>
<fonts count="4">
<font><sz val="10"/><name val="Calibri"/></font>
<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>
<font><b/><sz val="16"/><color rgb="FF1F2937"/><name val="Calibri"/></font>
<font><b/><sz val="10"/><name val="Calibri"/></font>
</fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF1F2937"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>
<border><left/><right/><top/><bottom style="thin"><color rgb="FFD1D5DB"/></bottom><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="7">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="top"/></xf>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"><alignment vertical="center" wrapText="1"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1"><alignment vertical="top" wrapText="1"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"><alignment vertical="top"/></xf>
<xf numFmtId="164" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"><alignment vertical="top"/></xf>
<xf numFmtId="9" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"><alignment vertical="top"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def to_xlsx(sheets: list[Sheet]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(sheets) + 1)) + "</Types>")
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{xml_escape(s.name)}" sheetId="{i}" r:id="rId{i}"/>' for i, s in enumerate(sheets, 1))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", STYLES)
        for i, s in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", s.xml())
    return buf.getvalue()


def _excel_date(ts: float, tz: int) -> float:
    return (ts + tz) / 86400 + 25569


def summary_sheet(title: str, filters: list, blocks: dict, tz: int) -> Sheet:
    sh = Sheet("Summary", [38, 22, 22, 18, 22])
    sh.add([title], 3)
    for k, v in filters:
        sh.add([k, v], [4, 0])
    sh.add(["Generated", fmt_time(time.time(), tz)], [4, 0])
    sh.add([])
    sh.add(["Key numbers"], 4)
    for k, v in blocks["kpis"]:
        sh.add([k, v], [0, 4])
    for name, header, data in (
        ("By category", ["Category", "Failures"], blocks["categories"]),
        (f"Failures per {blocks['per_label'].lower()}", [blocks["per_label"], "Failed", "Fixed on the next build"], blocks["per"]),
        ("Most frequent errors", ["Signature", "Times", "Pipelines", "Fixed", "Last seen"], blocks["errors"]),
        ("Pipelines that fail most", ["Pipeline", "Failed", "Fixed on the next build", "Still failing"], blocks["pipelines"]),
    ):
        sh.add([])
        sh.add([name], 4)
        sh.add(header, 1)
        for r in data:
            sh.add(list(r), 2)
    return sh


def failures_sheet(rows: list[dict], tz: int) -> Sheet:
    sh = Sheet("Failures", [c[2] for c in COLUMNS], freeze_header=True, autofilter=True)
    sh.add([c[1] for c in COLUMNS], 1)
    for r in rows:
        vals, styles = [], []
        for k, *_ in COLUMNS:
            v = r[k]
            if k == "when" and v:
                vals.append(_excel_date(v, tz)); styles.append(5)
            elif k == "confidence" and v is not None:
                vals.append(round(v, 2)); styles.append(6)
            else:
                vals.append(v); styles.append(2)
        sh.add(vals, styles)
    return sh


# ---------------------------------------------------------------- PDF
HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556,
        556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667,
        556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556,
        556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722,
        500, 500, 500, 334, 260, 334, 584]  # Helvetica advance widths for ASCII 32..126, per 1000 em
INK, MUTED, LINE, SOFT = (0.12, 0.16, 0.22), (0.42, 0.45, 0.5), (0.82, 0.84, 0.87), (0.96, 0.97, 0.98)
GOLD, RED, GREEN, BLUE = (0.78, 0.56, 0.12), (0.86, 0.15, 0.15), (0.09, 0.64, 0.29), (0.15, 0.39, 0.92)


def text_width(text: str, size: float, bold: bool = False, mono: bool = False) -> float:
    if mono:
        return len(text) * 0.6 * size
    w = sum(HELV[ord(c) - 32] if 32 <= ord(c) <= 126 else 556 for c in text)
    return w * size / 1000 * (1.06 if bold else 1)


def wrap(text: str, width: float, size: float, bold: bool = False, mono: bool = False) -> list[str]:
    lines: list[str] = []
    for para in str(text).replace("\r", "").split("\n"):
        cur = ""
        for word in para.split(" "):
            cand = word if not cur else cur + " " + word
            if text_width(cand, size, bold, mono) <= width:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
            while text_width(word, size, bold, mono) > width and len(word) > 1:  # a path or URL longer than the column
                k = len(word)
                while k > 1 and text_width(word[:k], size, bold, mono) > width:
                    k -= 1
                lines.append(word[:k])
                word = word[k:]
            cur = word
        lines.append(cur)
    return lines


class Pdf:
    def __init__(self, landscape: bool = True, margin: float = 32, title: str = "Build Forge"):
        self.w, self.h = (842.0, 595.0) if landscape else (595.0, 842.0)
        self.m, self.title = margin, title
        self.pages: list[list[str]] = []
        self.y = 0.0
        self.new_page()

    # drawing
    def new_page(self) -> None:
        self.pages.append([])
        self.y = self.h - self.m - 14

    @property
    def cur(self) -> list[str]:
        return self.pages[-1]

    @staticmethod
    def _s(text: str) -> str:
        data = str(text).encode("cp1252", "replace").decode("latin-1")
        return data.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def text(self, x: float, y: float, text: str, size: float = 9, bold: bool = False, color=INK, mono: bool = False) -> None:
        font = "F3" if mono else ("F2" if bold else "F1")
        self.cur.append(f"BT /{font} {size:.1f} Tf {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg {x:.1f} {y:.1f} Td ({self._s(text)}) Tj ET")

    def rect(self, x: float, y: float, w: float, h: float, fill=None, stroke=None) -> None:
        ops = []
        if fill:
            ops.append(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg")
        if stroke:
            ops.append(f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG 0.6 w")
        ops.append(f"{x:.1f} {y:.1f} {w:.1f} {h:.1f} re {'B' if fill and stroke else 'f' if fill else 'S'}")
        self.cur.append(" ".join(ops))

    def line(self, x1: float, y1: float, x2: float, y2: float, color=LINE) -> None:
        self.cur.append(f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG 0.6 w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def need(self, height: float) -> None:
        if self.y - height < self.m + 18:
            self.new_page()

    @property
    def inner(self) -> float:
        return self.w - 2 * self.m

    # building blocks
    def heading(self, text: str, size: float = 13, gap: float = 10) -> None:
        self.need(size + gap + 8)
        self.y -= gap
        self.text(self.m, self.y, text, size, bold=True)
        self.y -= 6

    def paragraph(self, text: str, size: float = 9, color=INK, width: float | None = None, x: float | None = None,
                  leading: float = 1.35, mono: bool = False) -> None:
        for ln in wrap(text, (width or self.inner), size, mono=mono):
            self.need(size * leading)
            self.y -= size * leading
            self.text(x if x is not None else self.m, self.y, ln, size, color=color, mono=mono)

    def kpi_grid(self, items: list[tuple[str, str]], cols: int = 4) -> None:
        gap = 10
        cw = (self.inner - gap * (cols - 1)) / cols
        ch = 46
        for i in range(0, len(items), cols):
            self.need(ch + gap)
            top = self.y
            for j, (label, value) in enumerate(items[i:i + cols]):
                x = self.m + j * (cw + gap)
                self.rect(x, top - ch, cw, ch, fill=SOFT, stroke=LINE)
                self.text(x + 9, top - 22, str(value), 15, bold=True)
                self.text(x + 9, top - 36, label[:44], 7.5, color=MUTED)
            self.y = top - ch - gap

    def bars(self, series: list[tuple[str, int, int]], height: float = 110) -> None:
        if not series:
            return
        self.need(height + 36)
        series = series[-62:]
        top, base = self.y, self.y - height
        mx = max([1] + [s[1] for s in series])
        slot = self.inner / len(series)
        bw = max(2.0, min(18.0, slot - 3))
        self.line(self.m, base, self.m + self.inner, base)
        for i, (label, failed, fixed) in enumerate(series):
            x = self.m + i * slot + (slot - bw) / 2
            h = (height - 6) * failed / mx
            self.rect(x, base, bw, h, fill=(0.93, 0.62, 0.62))
            if fixed:
                self.rect(x, base, bw, h * fixed / max(1, failed), fill=GREEN)
        step = max(1, len(series) // 10)
        for i in range(0, len(series), step):
            self.text(self.m + i * slot, base - 10, series[i][0][5:] if len(series[i][0]) == 10 else series[i][0], 6.5, color=MUTED)
        self.text(self.m + self.inner - 150, top - 2, "red: failed    green: fixed on the next build", 7, color=MUTED)
        self.y = base - 22

    def hbars(self, rows: list[tuple[str, int]], width: float = 330) -> None:
        mx = max([1] + [r[1] for r in rows])
        for label, n in rows:
            self.need(15)
            self.y -= 14
            self.text(self.m, self.y, label[:30], 8.5)
            self.rect(self.m + 120, self.y - 1, (width - 150) * n / mx, 8, fill=GOLD)
            self.text(self.m + 124 + (width - 150) * n / mx, self.y, str(n), 8, bold=True)

    def table(self, header: list[str], widths: list[float], rows: list[list[str]], size: float = 7.8, max_lines: int = 5) -> None:
        pad = 4
        scale = self.inner / sum(widths)
        widths = [w * scale for w in widths]

        def head():
            self.need(24)
            self.rect(self.m, self.y - 17, self.inner, 17, fill=INK)
            x = self.m
            for h, w in zip(header, widths):
                self.text(x + pad, self.y - 12, h, 7.5, bold=True, color=(1, 1, 1))
                x += w
            self.y -= 17

        head()
        for n, row in enumerate(rows):
            cells = []
            for val, w in zip(row, widths):
                lines = wrap(str(val), w - 2 * pad, size)
                if len(lines) > max_lines:
                    lines = lines[:max_lines]
                    lines[-1] = lines[-1][:max(0, len(lines[-1]) - 1)] + "…"
                cells.append(lines)
            rh = max(len(c) for c in cells) * size * 1.3 + 7
            if self.y - rh < self.m + 18:
                self.new_page()
                head()
            if n % 2:
                self.rect(self.m, self.y - rh, self.inner, rh, fill=SOFT)
            x = self.m
            for lines, w in zip(cells, widths):
                for k, ln in enumerate(lines):
                    self.text(x + pad, self.y - 4 - size * 1.15 - k * size * 1.3, ln, size)
                x += w
            self.y -= rh
            self.line(self.m, self.y, self.m + self.inner, self.y)

    # output
    def render(self, footer: str) -> bytes:
        total = len(self.pages)
        objs: list[bytes] = []

        def add(b: bytes) -> int:
            objs.append(b)
            return len(objs)

        add(b"<< /Type /Catalog /Pages 2 0 R >>")
        add(b"")  # pages, filled in below
        f1 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        f2 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
        f3 = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
        kids = []
        for n, ops in enumerate(self.pages, 1):
            body = list(ops)
            body.append(f"BT /F1 7 Tf {MUTED[0]} {MUTED[1]} {MUTED[2]} rg {self.m:.1f} {self.m - 8:.1f} Td ({self._s(footer)}) Tj ET")
            label = f"Page {n} of {total}"
            body.append(f"BT /F1 7 Tf {MUTED[0]} {MUTED[1]} {MUTED[2]} rg {self.w - self.m - text_width(label, 7):.1f} {self.m - 8:.1f} Td ({label}) Tj ET")
            stream = "\n".join(body).encode("latin-1", "replace")
            cid = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
            pid = add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.w:.0f} {self.h:.0f}] /Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R /F3 {f3} 0 R >> >> /Contents {cid} 0 R >>".encode())
            kids.append(pid)
        objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
        info = add(f"<< /Title ({self._s(self.title)}) /Producer (Build Forge) >>".encode())
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, o in enumerate(objs, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info {info} 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)


def cover(pdf: Pdf, title: str, filters: list, tz: int) -> None:
    pdf.rect(pdf.m, pdf.y - 3, 4, 26, fill=GOLD)
    pdf.text(pdf.m + 12, pdf.y + 2, title, 18, bold=True)
    pdf.y -= 14
    sub = "   ·   ".join(f"{k}: {v}" for k, v in filters) + f"   ·   Generated {fmt_time(time.time(), tz)}"
    for ln in wrap(sub, pdf.inner - 12, 8.5):
        pdf.y -= 12
        pdf.text(pdf.m + 12, pdf.y, ln, 8.5, color=MUTED)
    pdf.y -= 8


def to_pdf(title: str, filters: list, rows: list[dict] | None, blocks: dict | None, tz: int, truncated: int = 0) -> bytes:
    pdf = Pdf(landscape=True, title=title)
    cover(pdf, title, filters, tz)
    if blocks is not None:
        pdf.kpi_grid([(k, str(v)) for k, v in blocks["kpis"][:12]], 4)
        pdf.heading(f"Failures per {blocks['per_label'].lower()}")
        if blocks["per"]:
            pdf.bars(blocks["per"])
        else:
            pdf.paragraph("No failures in this period.", color=MUTED)
        pdf.heading("By category")
        pdf.hbars(blocks["categories"][:8])
        pdf.new_page()
        pdf.y -= 4
        pdf.heading("Most frequent errors", gap=2)
        pdf.table(["Signature", "Times", "Pipelines", "Fixed", "Last seen"], [260, 50, 60, 50, 110],
                  [[str(c) for c in r] for r in blocks["errors"]] or [["Nothing yet", "", "", "", ""]])
        pdf.heading("Pipelines that fail most")
        pdf.table(["Pipeline", "Failed", "Fixed on the next build", "Still failing"], [330, 60, 110, 70],
                  [[str(c) for c in r] for r in blocks["pipelines"]] or [["Nothing yet", "", "", ""]])
    if rows is not None:
        if blocks is not None:
            pdf.new_page()
        pdf.heading(f"Failed builds ({len(rows)}{'+' if truncated else ''})", gap=2)
        if truncated:
            pdf.paragraph(f"Only the first {len(rows)} failures are listed here; download the XLSX or CSV for the full list.",
                          color=MUTED, size=8)
        if rows:
            pdf.table(["Entry", "Pipeline", "Failed at", "Stage", "Category", "Error observed", "Outcome"],
                      [96, 135, 70, 80, 62, 220, 100],
                      [[r["id"], f"{r['job']} #{r['build']}", fmt_time(r["when"], tz), r["stage"], r["category"],
                        r["error_observed"], r["outcome"]] for r in rows])
        else:
            pdf.paragraph("No failed builds match these filters.", color=MUTED)
    return pdf.render(f"Build Forge · {title}")


# ---------------------------------------------------------------- the report entry points
def build(engine, f: dict, fmt: str, scope: str, sort: str, direction: str, tz: int) -> tuple[bytes, str, str]:
    if fmt not in ("csv", "xlsx", "pdf"):
        raise ValueError("format must be csv, xlsx or pdf")
    scope = scope if scope in ("list", "summary", "full") else "list"
    want_rows, want_summary = scope in ("list", "full"), scope in ("summary", "full")
    limit = MAX_PDF_ROWS if fmt == "pdf" else MAX_ROWS
    rows = truncated = None
    if want_rows:
        raw = engine.store.export_rows(f, sort, direction, limit + 1)
        truncated = max(0, len(raw) - limit)
        rows = [failure_row(i, tz) for i in raw[:limit]]
        if fmt != "pdf" and truncated:
            truncated = 0  # the +1 row only tells us the cap was hit
    blocks = summary_blocks(engine.store.stats(f, tz), tz) if want_summary else None
    title = {"list": "Failed builds", "summary": "Failure summary", "full": "Failure report"}[scope]
    filters = describe_filters(f, tz)
    stem = file_stem(f, tz, {"list": "failures", "summary": "summary", "full": "report"}[scope])
    if fmt == "csv":
        data = to_csv(title, filters, rows, blocks, tz)
    elif fmt == "xlsx":
        sheets = []
        if blocks is not None:
            sheets.append(summary_sheet(title, filters, blocks, tz))
        if rows is not None:
            sheets.append(failures_sheet(rows, tz))
        data = to_xlsx(sheets)
    else:
        data = to_pdf(title, filters, rows, blocks, tz, truncated or 0)
    return data, FORMATS[fmt], f"{stem}.{fmt}"


DECK_COLUMNS = [("id", "Card", 12, 0), ("title", "Title", 34, 0), ("status", "Status", 11, 0), ("category", "Category", 15, 0),
                ("signature", "Signature", 28, 0), ("times_seen", "Times seen", 11, 0), ("pipelines", "Pipelines", 10, 0),
                ("fixed_next", "Fixed on next build", 14, 0), ("used", "Answers given", 12, 0),
                ("last_seen", "Last seen", 17, 0), ("error_observed", "Error", 60, 0),
                ("proposed_solution", "Proposed solution", 90, 0), ("rebuild", "Rebuild can fix", 13, 0),
                ("notes", "Notes", 30, 0)]


def build_deck(engine, fmt: str, status: str, tz: int) -> tuple[bytes, str, str]:
    if fmt not in ("csv", "xlsx", "pdf", "json"):
        raise ValueError("format must be csv, xlsx, pdf or json")
    stem = f"buildforge-solutions-deck-{dt.date.today().isoformat()}"
    if fmt == "json":
        return json.dumps(engine.deck.export(), indent=2, ensure_ascii=False, default=str).encode(), FORMATS["json"], stem + ".json"
    _, cards, counts = engine.deck.list(status=status, sort="seen", limit=10000)
    for c in cards:
        c["rebuild"] = "yes" if c["rebuild_can_fix"] else "no"
        c["error_observed"] = plain(c.get("error_observed"))
        c["proposed_solution"] = plain(c.get("proposed_solution"))
    title = "Solutions deck"
    filters = [("Cards", f"{len(cards)} of {counts['all']}"), ("Ready to use", str(counts["usable"])),
               ("Approved", str(counts["approved"])), ("Draft", str(counts["draft"]))]
    if status:
        filters.append(("Status filter", status))
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\r\n")
        w.writerow([c[1] for c in DECK_COLUMNS])
        for c in cards:
            w.writerow([_csv_safe(fmt_time(c[k], tz) if k == "last_seen" else c.get(k)) if c.get(k) is not None else ""
                        for k, *_ in DECK_COLUMNS])
        return b"\xef\xbb\xbf" + out.getvalue().encode(), FORMATS["csv"], stem + ".csv"
    if fmt == "xlsx":
        sh = Sheet("Solutions", [c[2] for c in DECK_COLUMNS], freeze_header=True, autofilter=True)
        sh.add([c[1] for c in DECK_COLUMNS], 1)
        for c in cards:
            sh.add([(_excel_date(c["last_seen"], tz) if c.get("last_seen") else None) if k == "last_seen" else c.get(k)
                    for k, *_ in DECK_COLUMNS], [5 if k == "last_seen" else 2 for k, *_ in DECK_COLUMNS])
        return to_xlsx([sh]), FORMATS["xlsx"], stem + ".xlsx"
    pdf = Pdf(landscape=False, title=title)
    cover(pdf, title, filters, tz)
    pdf.paragraph("One card per kind of error. Each answer was written by the Kiro agent for an earlier failure and kept here "
                  "so it can be suggested again when the same or a similar error comes back.", color=MUTED, size=8.5)
    for c in cards:
        pdf.need(120)
        pdf.y -= 12
        pdf.rect(pdf.m, pdf.y - 20, pdf.inner, 22, fill=SOFT, stroke=LINE)
        pdf.text(pdf.m + 8, pdf.y - 14, f"{c['id']}   {c['title']}"[:90], 10.5, bold=True)
        badge = c["status"].upper()
        pdf.text(pdf.m + pdf.inner - text_width(badge, 8, True) - 8, pdf.y - 13, badge, 8, bold=True,
                 color=GREEN if c["status"] == "approved" else MUTED)
        pdf.y -= 24
        meta = (f"{CATEGORY_LABEL.get(c.get('category'), c.get('category') or 'Unknown')}  ·  seen {c['times_seen']}x in "
                f"{c['pipelines']} pipeline(s)  ·  fixed on the next build {c['fixed_next']}x  ·  used {c['used']}x"
                + (f"  ·  signature {c['signature']}" if c.get("signature") else ""))
        pdf.paragraph(meta, size=7.5, color=MUTED)
        pdf.paragraph("Error", size=7.5, color=GOLD)
        pdf.paragraph(c["error_observed"] or c["title"], size=9)
        pdf.paragraph("Solution", size=7.5, color=GOLD)
        pdf.paragraph(c["proposed_solution"], size=9)
        pdf.paragraph("A rebuild with the same parameters " + ("can fix this." if c["rebuild_can_fix"] else "will not fix this.")
                      + (f" {c['rebuild_reason']}" if c.get("rebuild_reason") else ""), size=8, color=MUTED)
        if c.get("pattern"):
            pdf.paragraph("Looks like", size=7.5, color=GOLD)
            pdf.paragraph("\n".join(c["pattern"].splitlines()[:4]), size=7, color=MUTED, mono=True)
        pdf.y -= 4
    return pdf.render(f"Build Forge · {title}"), FORMATS["pdf"], stem + ".pdf"
