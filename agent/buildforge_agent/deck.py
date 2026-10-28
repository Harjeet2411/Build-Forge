"""The solutions deck: one reusable answer per kind of error, learned from the Kiro agent's analyses.

Every analysis the Kiro agent submits is filed under its error signature. Recurring and similar errors therefore end up
on one card with the best answer so far, how often the error was seen, in how many pipelines and how often the next build
went green. Admins review the cards (draft -> approved) and can edit them. When Kiro is not available, or once the deck
is trusted enough (BF_DECK_MODE=first), a new failure that matches a card gets that card's answer instead.

Matching: an error with the same fingerprint as one the card already covers matches exactly; otherwise the words of the
normalised error lines are compared (IDF-weighted Jaccard) against every usable card.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import time

from .errors import fingerprint_lines
from .incidents import ALPHABET
from .store import Store, TS

STATUSES = ("draft", "approved", "retired")
WORD = re.compile(r"[a-z][a-z0-9_]{2,}")  # words, so paths and host names split into their parts
STOP = {"the", "and", "for", "with", "from", "that", "this", "not", "was", "are", "has", "have", "error", "failed",
        "fail", "failure", "line", "step", "stage", "pipeline", "build", "exit", "code", "fatal", "command"}
EDITABLE = ("title", "category", "error_observed", "proposed_solution", "rebuild_can_fix", "rebuild_reason", "status",
            "notes", "signature")


def new_id() -> str:
    return "SD-" + "".join(secrets.choice(ALPHABET) for _ in range(6))


def tokens(lines: list[str] | str) -> list[str]:
    text = "\n".join(lines) if isinstance(lines, list) else str(lines)
    return sorted({w for w in WORD.findall(text.lower()) if w not in STOP})


def pattern_of(excerpt: str) -> list[str]:
    return fingerprint_lines(excerpt or "")


def title_of(signature: str | None, error: str | None) -> str:
    if signature:
        return signature.replace("-", " ").replace("_", " ").strip().capitalize()
    return re.sub(r"[`*]", "", (error or "Error").strip().splitlines()[0])[:80] or "Error"


def _decode(row: dict | None) -> dict | None:
    if row is None:
        return None
    for col in ("tokens", "fingerprints"):
        try:
            row[col] = json.loads(row[col]) if row.get(col) else []
        except ValueError:
            row[col] = []
    row["rebuild_can_fix"] = bool(row.get("rebuild_can_fix"))
    return row


class Deck:
    def __init__(self, store: Store, mode: str = "fallback", threshold: float = 0.55, first_threshold: float = 0.85,
                 min_seen: int = 2):
        self.store = store
        self.mode = mode if mode in ("off", "fallback", "first") else "fallback"
        self.threshold = threshold
        self.first_threshold = first_threshold
        self.min_seen = min_seen

    # reading ----------------------------------------------------------------
    def _entries(self) -> list[dict]:
        return [_decode(r) for r in self.store._rows("SELECT * FROM solutions")]  # noqa: SLF001

    def _usage(self, entries: list[dict]) -> dict[str, dict]:
        """Live numbers per card, from the incidents that carry its signature or one of its fingerprints."""
        by_sig: dict[str, str] = {e["signature"]: e["id"] for e in entries if e.get("signature")}
        by_fp: dict[str, str] = {}
        for e in entries:
            for fp in e["fingerprints"]:
                by_fp.setdefault(fp, e["id"])
        out = {e["id"]: {"times_seen": 0, "pipelines": set(), "fixed": 0, "fixed_next": 0, "first_seen": None,
                         "last_seen": None, "used": 0, "last_incident": None, "last_job": None} for e in entries}
        rows = self.store._rows(  # noqa: SLF001
            f"SELECT id, job, signature, fingerprint, fixed_build, next_result, analysed_by, {TS} AS ts FROM incidents"
            " WHERE status='done' ORDER BY ts")
        for r in rows:
            sd_used = re.match(r"Solutions deck (SD-[A-Z0-9]{6})", r.get("analysed_by") or "")
            card = by_sig.get(r.get("signature") or "") or by_fp.get(r.get("fingerprint") or "")
            if sd_used and sd_used.group(1) in out:
                out[sd_used.group(1)]["used"] += 1
                card = card or sd_used.group(1)
            if not card or card not in out:
                continue
            u = out[card]
            u["times_seen"] += 1
            u["pipelines"].add(r["job"])
            u["fixed"] += 1 if r["fixed_build"] else 0
            u["fixed_next"] += 1 if r["next_result"] == "SUCCESS" else 0
            u["first_seen"] = u["first_seen"] or r["ts"]
            u["last_seen"], u["last_incident"], u["last_job"] = r["ts"], r["id"], r["job"]
        for u in out.values():
            u["pipelines"] = len(u["pipelines"])
        return out

    def _view(self, e: dict, u: dict) -> dict:
        e = dict(e)
        e.update(u)
        e["usable"] = self.usable(e)
        e.pop("tokens", None)
        e.pop("fingerprints", None)
        return e

    def usable(self, e: dict) -> bool:
        if e["status"] == "retired" or not (e.get("proposed_solution") or "").strip():
            return False
        return e["status"] == "approved" or (e.get("times_seen") or 0) >= self.min_seen

    def list(self, q: str = "", status: str = "", category: str = "", sort: str = "seen", direction: str = "desc",
             limit: int = 50, offset: int = 0) -> tuple[int, list[dict], dict]:
        entries = self._entries()
        usage = self._usage(entries)
        rows = [self._view(e, usage[e["id"]]) for e in entries]
        counts = {s: sum(1 for r in rows if r["status"] == s) for s in STATUSES}
        counts["usable"] = sum(1 for r in rows if r["usable"])
        counts["all"] = len(rows)
        needle = q.strip().lower()
        if needle:
            rows = [r for r in rows if needle in " ".join(str(r.get(k) or "") for k in (
                "id", "signature", "title", "category", "error_observed", "proposed_solution", "pattern", "notes")).lower()]
        if status == "usable":
            rows = [r for r in rows if r["usable"]]
        elif status:
            rows = [r for r in rows if r["status"] == status]
        if category:
            rows = [r for r in rows if r.get("category") == category]
        key = {"seen": lambda r: r["times_seen"], "pipelines": lambda r: r["pipelines"],
               "fixed": lambda r: r["fixed_next"], "updated": lambda r: r["updated"], "used": lambda r: r["used"],
               "title": lambda r: (r["title"] or "").lower(), "status": lambda r: r["status"],
               "last": lambda r: r["last_seen"] or 0, "category": lambda r: r.get("category") or ""}.get(sort)
        rows.sort(key=key or (lambda r: r["times_seen"]), reverse=direction != "asc")
        return len(rows), rows[offset:offset + limit], counts

    def get(self, sid: str) -> dict | None:
        rows = [_decode(r) for r in self.store._rows("SELECT * FROM solutions WHERE id=?", (sid,))]  # noqa: SLF001
        if not rows:
            return None
        entry = rows[0]
        entries = self._entries()
        usage = self._usage(entries)
        view = self._view(entry, usage[sid])
        sigs, fps = entry.get("signature"), entry["fingerprints"]
        cond, args = [], []
        if sigs:
            cond.append("signature=?")
            args.append(sigs)
        if fps:
            cond.append("fingerprint IN (" + ",".join("?" * len(fps)) + ")")
            args += fps
        view["pattern"] = entry.get("pattern") or ""
        view["incidents"] = self.store._rows(  # noqa: SLF001
            f"SELECT id, job, build, build_url, status, next_result, fixed_build, {TS} AS ts, analysed_by FROM incidents"
            f" WHERE ({' OR '.join(cond) or '0'}) ORDER BY ts DESC LIMIT 25", tuple(args))
        return view

    # matching ---------------------------------------------------------------
    def match(self, excerpt: str, fingerprint: str | None = None, signature: str | None = None,
              minimum: float | None = None, usable_only: bool = True) -> dict | None:
        """The card that best answers this error, with the score (1.0 = same fingerprint) and how it matched."""
        entries = self._entries()
        if not entries:
            return None
        usage = self._usage(entries)
        cards = [{**self._view(e, usage[e["id"]]), "_tokens": set(e["tokens"]), "_fps": set(e["fingerprints"])}
                 for e in entries]
        pool = [c for c in cards if c["usable"]] if usable_only else [c for c in cards if c["status"] != "retired"]
        if not pool:
            return None
        if fingerprint:
            for c in pool:
                if fingerprint in c["_fps"]:
                    return self._hit(c, 1.0, "same error")
        want = set(tokens(pattern_of(excerpt) or excerpt))
        if not want:
            return None
        df: dict[str, int] = {}
        for c in cards:
            for t in c["_tokens"] | want:
                df[t] = df.get(t, 0) + 1
        n = len(cards) + 1

        def weight(t: str) -> float:
            return math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0

        best, best_score = None, 0.0
        for c in pool:
            if not c["_tokens"]:
                continue
            inter = sum(weight(t) for t in c["_tokens"] & want)
            union = sum(weight(t) for t in c["_tokens"] | want)
            score = inter / union if union else 0.0
            if signature and c.get("signature") == signature:
                score = max(score, 0.99)
            if score > best_score:
                best, best_score = c, score
        if best and best_score >= (self.threshold if minimum is None else minimum):
            return self._hit(best, round(best_score, 3), "similar error")
        return None

    @staticmethod
    def _hit(card: dict, score: float, how: str) -> dict:
        card = {k: v for k, v in card.items() if not k.startswith("_")}
        card["score"], card["how"] = score, how
        return card

    def answer(self, excerpt: str, fingerprint: str | None, history: dict, first: bool = False) -> dict | None:
        """An analysis taken from the deck for this error, or None. `first` = the deck is asked before Kiro."""
        if self.mode == "off" or (first and self.mode != "first"):
            return None
        hit = self.match(excerpt, fingerprint, minimum=self.first_threshold if first else None)
        if not hit or (first and hit["status"] != "approved"):
            return None
        came_back = bool(history.get("rebuilt_and_failed_again"))
        pct = round(hit["score"] * 100)
        note = ("same error as" if hit["how"] == "same error" else f"{pct}% similar to") + f" {hit['times_seen']} earlier failure(s)"
        return {
            "error_observed": hit["error_observed"] or hit["title"],
            "proposed_solution": hit["proposed_solution"],
            "category": hit["category"] or "unknown",
            "severity": "medium",
            "confidence": round(min(0.95, hit["score"] * (0.9 if hit["status"] == "approved" else 0.75)), 2),
            "rebuild_can_fix": bool(hit["rebuild_can_fix"]) and not came_back,
            "rebuild_reason": (hit["rebuild_reason"] or "") if not came_back else "This error came back after a rebuild.",
            "signature": hit["signature"] or hit["id"],
            "source": "deck", "deck_id": hit["id"], "deck_match": hit["score"], "deck_note": note,
            "deck_status": hit["status"],
        }

    # learning ---------------------------------------------------------------
    def record(self, inc: dict) -> None:
        """File an analysis the Kiro agent just submitted under its error signature (creating the card if new)."""
        a = inc.get("analysis") or {}
        if inc.get("status") != "done" or a.get("source") == "deck" or not a.get("proposed_solution"):
            return
        sig = a.get("signature") or None
        pattern = pattern_of(inc.get("excerpt") or "")
        fp = inc.get("fingerprint")
        row = None
        if sig:
            rows = self.store._rows("SELECT * FROM solutions WHERE signature=? ORDER BY created LIMIT 1", (sig,))  # noqa: SLF001
            row = _decode(rows[0]) if rows else None
        if row is None and fp:
            for e in self._entries():
                if fp in e["fingerprints"]:
                    row = e
                    break
        now = time.time()
        conf = float(a["confidence"]) if a.get("confidence") is not None else 0.0
        fps = ([fp] if fp else [])
        if row is None:
            self.store._exec(  # noqa: SLF001
                "INSERT INTO solutions(id, signature, title, category, error_observed, proposed_solution, rebuild_can_fix,"
                " rebuild_reason, confidence, pattern, tokens, fingerprints, status, source, last_incident, created,"
                " updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id(), sig, title_of(sig, a.get("error_observed")), a.get("category"), a.get("error_observed"),
                 a["proposed_solution"], 1 if a.get("rebuild_can_fix") else 0, a.get("rebuild_reason"), conf,
                 "\n".join(pattern), json.dumps(tokens(pattern)), json.dumps(fps), "draft", "auto", inc["id"], now, now))
            return
        known = list(row["fingerprints"])
        for f in fps:
            if f not in known:
                known.append(f)
        changes: dict = {"fingerprints": json.dumps(known[-50:]), "last_incident": inc["id"], "updated": now}
        if not row.get("signature") and sig:
            changes["signature"] = sig
        if row["status"] == "draft" and row.get("source") == "auto" and conf >= float(row.get("confidence") or 0):
            changes.update(error_observed=a.get("error_observed"), proposed_solution=a["proposed_solution"],
                           rebuild_can_fix=1 if a.get("rebuild_can_fix") else 0, rebuild_reason=a.get("rebuild_reason"),
                           category=a.get("category") or row.get("category"), confidence=conf)
        if pattern and len(pattern) > len((row.get("pattern") or "").splitlines()) and row["status"] == "draft":
            changes.update(pattern="\n".join(pattern), tokens=json.dumps(tokens(pattern)))
        self._update(row["id"], changes)

    def backfill(self) -> int:
        """Build the deck from the analyses already stored (runs once, when the deck is empty)."""
        if self.store.scalar("SELECT COUNT(*) FROM solutions"):
            return 0
        n = 0
        for inc in self.store._rows("SELECT * FROM incidents WHERE status='done' ORDER BY COALESCE(failed_at, created)"):  # noqa: SLF001
            self.record(inc)
            n += 1
        return n

    # editing ----------------------------------------------------------------
    def _update(self, sid: str, changes: dict) -> None:
        cols = ", ".join(f"{k}=?" for k in changes)
        self.store._exec(f"UPDATE solutions SET {cols} WHERE id=?", (*changes.values(), sid))  # noqa: SLF001

    def update(self, sid: str, fields: dict, user: str = "") -> dict | None:
        if not self.store.scalar("SELECT COUNT(*) FROM solutions WHERE id=?", (sid,)):
            return None
        changes = {}
        for k in EDITABLE:
            if k not in fields:
                continue
            v = fields[k]
            if k == "status" and v not in STATUSES:
                raise ValueError("status must be draft, approved or retired")
            if k == "rebuild_can_fix":
                v = 1 if v in (True, 1, "1", "true", "yes") else 0
            elif v is not None:
                v = str(v).strip()
                if k == "title" and not v:
                    raise ValueError("a title is required")
            changes[k] = v
        if "proposed_solution" in changes or "error_observed" in changes:
            changes["source"] = "manual"  # a person has worded it: Kiro answers no longer overwrite the card
        changes.update(edited_by=user or None, updated=time.time())
        self._update(sid, changes)
        return self.get(sid)

    def create(self, fields: dict, user: str = "") -> dict:
        title = str(fields.get("title") or "").strip()
        solution = str(fields.get("proposed_solution") or "").strip()
        if not title or not solution:
            raise ValueError("a title and a proposed solution are required")
        pattern = str(fields.get("pattern") or fields.get("error_text") or "")
        lines = pattern_of(pattern) or [l.strip() for l in pattern.splitlines() if l.strip()][-12:]
        now, sid = time.time(), new_id()
        sig = str(fields.get("signature") or "").strip() or None
        self.store._exec(  # noqa: SLF001
            "INSERT INTO solutions(id, signature, title, category, error_observed, proposed_solution, rebuild_can_fix,"
            " rebuild_reason, confidence, pattern, tokens, fingerprints, status, source, notes, edited_by, created,"
            " updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, sig, title, fields.get("category") or None, str(fields.get("error_observed") or title), solution,
             1 if fields.get("rebuild_can_fix") in (True, 1, "1", "true", "yes") else 0, fields.get("rebuild_reason"), 1.0,
             "\n".join(lines), json.dumps(tokens(lines)), "[]", "approved" if fields.get("status") == "approved" else "draft",
             "manual", fields.get("notes"), user or None, now, now))
        return self.get(sid)

    # portability ------------------------------------------------------------
    def export(self) -> dict:
        entries = self._entries()
        usage = self._usage(entries)
        items = []
        for e in entries:
            v = self._view(e, usage[e["id"]])
            v["pattern"] = e.get("pattern") or ""
            v["tokens"] = e["tokens"]
            items.append(v)
        items.sort(key=lambda r: -r["times_seen"])
        return {"format": "buildforge-solutions-deck/1", "exported": time.time(), "count": len(items), "solutions": items}

    def import_(self, data: dict, user: str = "") -> dict:
        """Add the cards of an exported deck (matched on signature; an existing card is left alone)."""
        added = skipped = 0
        for s in data.get("solutions") or []:
            sig = (s.get("signature") or "").strip() or None
            if not s.get("title") or not s.get("proposed_solution"):
                skipped += 1
                continue
            if sig and self.store.scalar("SELECT COUNT(*) FROM solutions WHERE signature=?", (sig,)):
                skipped += 1
                continue
            pattern = str(s.get("pattern") or "")
            lines = [l for l in pattern.splitlines() if l.strip()]
            now = time.time()
            self.store._exec(  # noqa: SLF001
                "INSERT INTO solutions(id, signature, title, category, error_observed, proposed_solution, rebuild_can_fix,"
                " rebuild_reason, confidence, pattern, tokens, fingerprints, status, source, notes, edited_by, created,"
                " updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id(), sig, str(s["title"])[:200], s.get("category"), s.get("error_observed"),
                 s["proposed_solution"], 1 if s.get("rebuild_can_fix") else 0, s.get("rebuild_reason"),
                 float(s.get("confidence") or 0.5), pattern, json.dumps(tokens(lines)), json.dumps(s.get("fingerprints") or []),
                 s.get("status") if s.get("status") in STATUSES else "draft", "import", s.get("notes"), user or None, now, now))
            added += 1
        return {"added": added, "skipped": skipped}
