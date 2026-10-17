"""Read-only queries on books.db."""

from __future__ import annotations

import difflib
import json
import re
import sqlite3
import threading
from pathlib import Path

from server.normalize import (
    SUBJECT_NAMES,
    NormalizeError,
    normalize_chapter_id,
    normalize_class,
    normalize_subject,
    parse_query,
)

PAGE_MARK = re.compile(r"<!-- page (\d+) -->")
STOPWORDS = {
    "a", "an", "the", "of", "on", "in", "about", "and", "or", "to", "for", "with", "is", "are", "was", "be",
    "chapter", "chapters", "ch", "lesson", "one", "that", "this", "which", "what", "topic", "book", "books",
    "new", "old", "ncert", "unit", "from", "where", "we", "learn", "study", "do", "does", "how", "why",
    "class", "std", "grade", "part", "it", "its", "all", "some", "any", "me", "please", "find", "give",
    "science", "maths", "math", "sst", "social", "s", "called", "named", "wala", "vala", "waala",
}
CONF_RANK = {"high": 3, "medium": 2, "low": 1}


class BooksUnavailable(RuntimeError):
    pass


def content_tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", text.lower().replace("’", "'"))
    out = []
    for t in toks:
        if t in STOPWORDS or (len(t) < 2 and not t.isdigit()):
            continue
        if t not in out:
            out.append(t)
    return out


def strip_md(text: str) -> str:
    text = PAGE_MARK.sub(" ", text)
    text = re.sub(r"[#*>|_`]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _fuzzy_contains(tokens: list[str], target_tokens: list[str]) -> bool:
    """Every query token matches some target token (allowing small typos)."""
    if not tokens:
        return False
    for t in tokens:
        if not any(t == w or (len(t) > 3 and difflib.SequenceMatcher(None, t, w).ratio() >= 0.8) for w in target_tokens):
            return False
    return True


class Books:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._local = threading.local()

    # -- plumbing -------------------------------------------------------------------------
    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if not self.path.exists():
                raise BooksUnavailable(
                    "The textbook database isn't installed on the server yet, so I can't look up chapters. "
                    "Tell the teacher the book data is still being set up."
                )
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def available(self) -> bool:
        return self.path.exists()

    def stats(self) -> dict:
        rows = self.db.execute("SELECT class, subject, COUNT(*) n FROM chapters GROUP BY class, subject").fetchall()
        meta = dict(self.db.execute("SELECT key, value FROM meta").fetchall())
        return {"chapters": {f"{r['class']}-{r['subject']}": r["n"] for r in rows}, "built_at": meta.get("built_at")}

    def _chapter_rows(self, cls: int | None = None, subject: str | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM chapters WHERE 1=1", []
        if cls:
            q += " AND class=?"
            args.append(cls)
        if subject:
            q += " AND subject=?"
            args.append(subject)
        return self.db.execute(q + " ORDER BY class, subject, chapter_no", args).fetchall()

    def chapter(self, chapter_id: str) -> sqlite3.Row:
        cid = normalize_chapter_id(chapter_id)
        row = self.db.execute("SELECT * FROM chapters WHERE chapter_id=?", (cid,)).fetchone()
        if row is None:
            cls, subj, no = cid.split("-")
            have = [r["chapter_no"] for r in self._chapter_rows(int(cls), subj)]
            if have:
                raise LookupError(
                    f"There is no chapter {int(no)} in Class {cls} {SUBJECT_NAMES[subj]}. "
                    f"That book has chapters {min(have)}-{max(have)}. Use list_chapters to see them."
                )
            raise LookupError(f"I don't have Class {cls} {SUBJECT_NAMES[subj]} loaded yet.")
        return row

    @staticmethod
    def _brief(row: sqlite3.Row) -> dict:
        return {
            "chapter_id": row["chapter_id"],
            "class": row["class"],
            "subject": row["subject"],
            "book": row["book"],
            "part": row["part"],
            "chapter_no": row["chapter_no"],
            "title": row["title"],
        }

    # -- list / pack / section ---------------------------------------------------------------
    def list_chapters(self, class_: object, subject: object) -> dict:
        cls, subj = normalize_class(class_), normalize_subject(subject)
        rows = self._chapter_rows(cls, subj)
        if not rows:
            raise LookupError(f"I don't have Class {cls} {SUBJECT_NAMES[subj]} loaded yet.")
        out = []
        for r in rows:
            item = self._brief(r)
            summary = strip_md(r["summary"] or "")
            item["one_line"] = re.split(r"(?<=[.!?])\s", summary, maxsplit=1)[0][:200] if summary else ""
            out.append(item)
        return {"class": cls, "subject": subj, "book": rows[0]["book"], "chapters": out}

    def chapter_pack(self, chapter_id: str) -> dict:
        row = self.chapter(chapter_id)
        cid = row["chapter_id"]
        sections = []
        for s in self.db.execute("SELECT * FROM sections WHERE chapter_id=? ORDER BY ord", (cid,)):
            preview = strip_md(s["text"])
            if preview.lower().startswith(strip_md(s["heading"]).lower()):
                preview = preview[len(strip_md(s["heading"])):].strip()
            sections.append({
                "section_id": s["section_id"],
                "heading": s["heading"],
                "pages": _pages(s["page_start"], s["page_end"]),
                "preview": _cut(preview, 220),
            })

        def rows(sql: str) -> list[dict]:
            return [dict(r) for r in self.db.execute(sql, (cid,))]

        return {
            **self._brief(row),
            "pages": _pages(row["page_start"], row["page_end"]),
            "summary": row["summary"],
            "topics": json.loads(row["topics_json"] or "[]"),
            "sections": sections,
            "key_terms": rows("SELECT term, meaning, page FROM key_terms WHERE chapter_id=?"),
            "activities": rows("SELECT label, description, page FROM activities WHERE chapter_id=? ORDER BY ord"),
            "figures": rows("SELECT label, caption, page FROM figures WHERE chapter_id=? ORDER BY ord"),
            "exercises": rows("SELECT label, text, type_hint, page, source FROM exercises WHERE chapter_id=? ORDER BY ord"),
            "structured_by": row["structured_by"],
        }

    def get_section(self, chapter_id: str, section_id: str) -> dict:
        row = self.chapter(chapter_id)
        cid = row["chapter_id"]
        secs = self.db.execute("SELECT * FROM sections WHERE chapter_id=? ORDER BY ord", (cid,)).fetchall()
        want = str(section_id).strip()
        hit = next((s for s in secs if s["section_id"].lower() == want.lower()), None)
        if hit is None:  # forgiving: "s3", "3", "2.1", or words from the heading
            m = re.fullmatch(r"(?:s|sec|section)?\s*0*(\d+)", want.lower())
            if m:
                hit = next((s for s in secs if s["section_id"].endswith(f"-s{int(m.group(1)):02d}")), None)
        if hit is None:
            hit = next((s for s in secs if re.match(rf"\W*{re.escape(want)}\b", strip_md(s["heading"]))), None)
        if hit is None:
            toks = content_tokens(want)
            scored = [(difflib.SequenceMatcher(None, want.lower(), strip_md(s["heading"]).lower()).ratio()
                       + (1 if toks and _fuzzy_contains(toks, content_tokens(s["heading"])) else 0), s) for s in secs]
            scored.sort(key=lambda x: -x[0])
            if scored and scored[0][0] >= 0.6:
                hit = scored[0][1]
        if hit is None:
            listing = "; ".join(f"{s['section_id']} = {strip_md(s['heading'])}" for s in secs)
            raise LookupError(f"No section {section_id!r} in {cid}. Sections are: {listing}")
        return {
            **self._brief(row),
            "section_id": hit["section_id"],
            "heading": hit["heading"],
            "pages": _pages(hit["page_start"], hit["page_end"]),
            "text": hit["text"],
        }

    # -- search -----------------------------------------------------------------------------
    def search(self, query: str, class_=None, subject=None, chapter_id=None, limit: int = 8) -> dict:
        cls = normalize_class(class_) if class_ not in (None, "") else None
        subj = normalize_subject(subject) if subject not in (None, "") else None
        cid = self.chapter(chapter_id)["chapter_id"] if chapter_id not in (None, "") else None
        toks = content_tokens(query) or re.findall(r"[a-z0-9]+", query.lower())
        limit = max(1, min(int(limit or 8), 25))
        if not toks:
            raise NormalizeError("Give me some words to search for, e.g. 'photosynthesis' or 'Harappan cities'.")
        hits = self._fts_sections(toks, cls, subj, cid, limit * 3, mode="AND")
        mode = "all words"
        if not hits and len(toks) > 1:
            hits = self._fts_sections(toks, cls, subj, cid, limit * 3, mode="OR")
            mode = "some words"
        results = []
        for h in hits[:limit]:
            page = _page_of_match(h["text"], toks) or h["page_start"]
            results.append({
                "chapter_id": h["chapter_id"],
                "chapter_title": h["title"],
                "section_id": h["section_id"],
                "heading": h["heading"],
                "page": page,
                "snippet": h["snip"],
            })
        return {"query": query, "matched": mode if results else "nothing", "results": results}

    def _fts_sections(self, toks, cls, subj, cid, limit, mode="AND") -> list[sqlite3.Row]:
        match = f" {mode} ".join(f'"{t}"' for t in toks)
        q = (
            "SELECT f.chapter_id, f.section_id, s.heading, s.text, s.page_start, c.title, c.class, c.subject, "
            "snippet(fts_sections, 0, '**', '**', ' … ', 28) AS snip, bm25(fts_sections, 1.0, 2.0) AS score "
            "FROM fts_sections f JOIN sections s ON s.section_id=f.section_id "
            "JOIN chapters c ON c.chapter_id=f.chapter_id WHERE fts_sections MATCH ?"
        )
        args: list = [match]
        if cls:
            q += " AND c.class=?"
            args.append(cls)
        if subj:
            q += " AND c.subject=?"
            args.append(subj)
        if cid:
            q += " AND f.chapter_id=?"
            args.append(cid)
        q += " ORDER BY score LIMIT ?"
        args.append(limit)
        return self.db.execute(q, args).fetchall()

    def _chapters_matching(self, table: str, token: str) -> set[str]:
        return {r[0] for r in self.db.execute(f"SELECT DISTINCT chapter_id FROM {table} WHERE {table} MATCH ?", (f'"{token}"',))}

    # -- find_chapter -----------------------------------------------------------------------
    def find_chapter(self, query: str, class_=None, subject=None) -> dict:
        parsed = parse_query(query)
        cls = normalize_class(class_) if class_ not in (None, "") else parsed["class"]
        subj = normalize_subject(subject) if subject not in (None, "") else parsed["subject"]
        if cls is not None:
            cls = normalize_class(cls)
        notes: list[str] = []

        # 1. Exact chapter number.
        if parsed["chapter_no"] is not None and not content_tokens(parsed["rest"]):
            return self._by_number(parsed["chapter_no"], cls, subj)

        rest = parsed["rest"] or str(query)
        results = self._resolve_text(rest, cls, subj)
        best = max((CONF_RANK[r["confidence"]] for r in results), default=0)

        # Wrong class / subject? Look everywhere if the scoped search was weak.
        if (cls or subj) and best < CONF_RANK["high"]:
            wider = [r for r in self._resolve_text(rest, None, None) if (cls and r["class"] != cls) or (subj and r["subject"] != subj)]
            wider = [r for r in wider if CONF_RANK[r["confidence"]] > best]
            for r in wider[:2]:
                where = f"Class {r['class']} {SUBJECT_NAMES[r['subject']]}"
                r["match_reason"] += f" (found in {where}, not in what was asked)"
                notes.append(
                    f"This topic isn't a strong match in the class/subject asked; it is in {where}, "
                    f"Ch {r['chapter_no']} '{r['title']}'. Check with the teacher which she wants."
                )
            results = wider[:2] + results

        # Merge duplicates, keep the best reason per chapter.
        merged: dict[str, dict] = {}
        for r in results:
            key = r.get("chapter_id") or f"none:{r.get('old_title')}"
            if key not in merged or (CONF_RANK[r["confidence"]], r.get("score", 0)) > (
                CONF_RANK[merged[key]["confidence"]], merged[key].get("score", 0)
            ):
                merged[key] = r
        ranked = sorted(merged.values(), key=lambda r: (-CONF_RANK[r["confidence"]], -r.get("score", 0)))[:3]
        # One clear winner: weaker topic matches below it are only "medium".
        strong = ("title match", "matched old chapter name", "chapter number")
        if ranked and ranked[0]["confidence"] == "high" and ranked[0]["match_reason"].startswith(strong):
            for r in ranked[1:]:
                if r["confidence"] == "high" and not r["match_reason"].startswith(strong):
                    r["confidence"] = "medium"
        notes = list(dict.fromkeys(notes))
        for r in ranked:
            r.pop("score", None)
        if not ranked or all(r["confidence"] == "low" for r in ranked):
            notes.append(
                "No chapter in the new NCERT books for this class/subject clearly covers this. "
                "It may not be in the syllabus. Tell the teacher instead of guessing."
            )
        return {"query": query, "class": cls, "subject": subj, "matches": ranked, "notes": notes}

    def _by_number(self, no: int, cls, subj) -> dict:
        rows = [r for r in self._chapter_rows(cls, subj)]
        if no == -1:  # "last chapter"
            if cls and subj and rows:
                no = max(r["chapter_no"] for r in rows)
            else:
                raise NormalizeError("For 'last chapter', tell me the class and subject too.")
        hits = [r for r in rows if r["chapter_no"] == no]
        notes = []
        if not hits:
            if cls and subj and rows:
                raise LookupError(
                    f"Class {cls} {SUBJECT_NAMES[subj]} has chapters 1-{max(r['chapter_no'] for r in rows)}, "
                    f"so there is no chapter {no}."
                )
            raise LookupError(f"I couldn't find a chapter {no} for that class/subject.")
        conf = "high" if cls and subj else "medium"
        if conf != "high":
            notes.append("Class or subject wasn't given, so several books have this chapter number. Ask which one.")
        matches = [
            {**self._brief(r), "match_reason": f"chapter number {no}", "confidence": conf}
            for r in hits
        ]
        if any(r["part"] == 2 for r in hits):
            notes.append("Chapter numbers run on across Part 1 and Part 2 of this book, the way students count them.")
        return {"query": str(no), "class": cls, "subject": subj, "matches": matches[:6], "notes": notes}

    def _resolve_text(self, text: str, cls, subj) -> list[dict]:
        toks = content_tokens(text)
        if not toks:
            raise NormalizeError(
                f"I couldn't tell which chapter {text!r} means. Give a chapter number, title, old chapter name or topic."
            )
        rows = self._chapter_rows(cls, subj)
        results: list[dict] = []
        q = " ".join(toks)

        # 2. Title match (typos allowed).
        for r in rows:
            title_toks = content_tokens(r["title"])
            ratio = difflib.SequenceMatcher(None, q, " ".join(title_toks)).ratio()
            if ratio >= 0.85 or (_fuzzy_contains(toks, title_toks) and len(toks) >= min(2, len(title_toks))):
                results.append({**self._brief(r), "match_reason": "title match", "confidence": "high", "score": 10 + ratio})
            elif ratio >= 0.7 or _fuzzy_contains(toks, title_toks):
                results.append({**self._brief(r), "match_reason": "title partly matches", "confidence": "medium", "score": 5 + ratio})

        # 3. Old NCERT chapter names.
        results += self._old_map(q, toks, cls, subj)

        # 4. Titles / topics / summaries, then 5. section text.
        results += self._fts_rank("fts_chapters", toks, cls, subj, "matched chapter topics", base=3)
        results += self._fts_rank("fts_sections", toks, cls, subj, "matched text inside the chapter", base=0)
        return results

    def _old_map(self, q: str, toks: list[str], cls, subj) -> list[dict]:
        out = []
        for m in self.db.execute("SELECT * FROM old_map"):
            old_toks = content_tokens(m["old_title"])
            ratio = difflib.SequenceMatcher(None, q, " ".join(old_toks)).ratio()
            if not (ratio >= 0.85 or (len(toks) >= 2 and _fuzzy_contains(toks, old_toks)) or (len(old_toks) == 1 and toks == old_toks)):
                continue
            if m["new_chapter_id"] is None:
                out.append({
                    "chapter_id": None, "old_title": m["old_title"], "confidence": "high", "score": 9,
                    "class": m["old_class"], "subject": m["old_subject"], "title": None, "chapter_no": None,
                    "match_reason": f"matched old chapter name '{m['old_title']}'",
                    "tell_teacher": f"'{m['old_title']}' was in the old book but is not in the new NCERT syllabus. {m['note'] or ''}".strip(),
                })
                continue
            row = self.db.execute("SELECT * FROM chapters WHERE chapter_id=?", (m["new_chapter_id"],)).fetchone()
            if row is None:
                continue
            if (cls and row["class"] != cls) or (subj and row["subject"] != subj):
                conf = "medium"
            else:
                conf = "high" if ratio >= 0.85 or _fuzzy_contains(old_toks, toks) else "medium"
            item = {**self._brief(row), "confidence": conf, "score": 9 + ratio,
                    "match_reason": f"matched old chapter name '{m['old_title']}'"}
            old_cls = f"Class {m['old_class']} " if m["old_class"] else ""
            item["tell_teacher"] = (
                f"This was '{m['old_title']}' in the old {old_cls}book; it is now Class {row['class']} "
                f"Ch {row['chapter_no']} '{row['title']}'." + (f" ({m['note']})" if m["note"] else "")
            )
            out.append(item)
        return out

    def _fts_rank(self, table: str, toks: list[str], cls, subj, reason: str, base: float) -> list[dict]:
        per_token = {t: self._chapters_matching(table, t) for t in toks}
        allowed = {r["chapter_id"]: r for r in self._chapter_rows(cls, subj)}
        # BM25 over an OR query, summed per chapter (top 5 hits each).
        match = " OR ".join(f'"{t}"' for t in toks)
        weights = "1.0, 3.0" if table == "fts_sections" else "4.0, 3.0, 1.0"
        scores: dict[str, list[float]] = {}
        for r in self.db.execute(f"SELECT chapter_id, bm25({table}, {weights}) s FROM {table} WHERE {table} MATCH ?", (match,)):
            if r["chapter_id"] in allowed:
                scores.setdefault(r["chapter_id"], []).append(-r["s"])
        phrase = set()
        if len(toks) > 1:
            phrase = {r[0] for r in self.db.execute(f"SELECT DISTINCT chapter_id FROM {table} WHERE {table} MATCH ?",
                                                     ('"' + " ".join(toks) + '"',))}
        out = []
        for cid, ss in scores.items():
            coverage = sum(cid in per_token[t] for t in toks) / len(toks)
            strength = sum(sorted(ss, reverse=True)[:5])
            hits = len(ss)
            if table == "fts_chapters":
                conf = "high" if coverage == 1 else "medium" if coverage >= 0.5 else "low"
            else:
                if coverage == 1 and (hits >= 3 or cid in phrase):
                    conf = "medium"
                elif coverage == 1 or coverage >= 0.67:
                    conf = "low" if len(toks) > 2 and coverage < 1 else "medium" if hits >= 2 else "low"
                else:
                    conf = "low"
            out.append({**self._brief(allowed[cid]), "match_reason": reason, "confidence": conf,
                        "score": base + coverage * 2 + min(strength, 50) / 25})
        # Topic named strongly in one chapter's text: promote the clear winner.
        if table == "fts_sections" and out:
            out.sort(key=lambda r: -r["score"])
            if len(out) == 1 or out[0]["score"] > out[1]["score"] + 0.6:
                if out[0]["confidence"] == "medium":
                    out[0]["confidence"] = "high"
                    out[0]["match_reason"] = "this topic is taught in this chapter"
        return out


def _pages(a, b) -> str:
    if a and b and a != b:
        return f"{a}-{b}"
    return str(a or b or "")


def _cut(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut + " …"


def _page_of_match(text: str, toks: list[str]) -> int | None:
    low = text.lower()
    pos = -1
    for t in toks:
        stem = t[:5] if len(t) > 5 else t
        i = low.find(stem)
        if i >= 0 and (pos < 0 or i < pos):
            pos = i
    if pos < 0:
        return None
    marks = [(m.start(), int(m.group(1))) for m in PAGE_MARK.finditer(text)]
    before = [p for s, p in marks if s <= pos]
    if before:
        return before[-1]
    return None
