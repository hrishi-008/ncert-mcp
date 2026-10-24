"""Stage 4: data/structured/*.json + data/old_to_new_map.json -> data/books.db (with FTS5 search).

Print artefacts (margin icons, heading debris) are removed on the way in: see ingest/cleanup.py.

Usage:
    python -m ingest.build_db
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from ingest.cleanup import clean_chapter
from ingest.common import BOOKS_DB, OLD_MAP, STRUCTURED
from ingest.schema import Chapter

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE chapters(
    chapter_id TEXT PRIMARY KEY, class INTEGER, subject TEXT, book TEXT, part INTEGER,
    chapter_no INTEGER, title TEXT, summary TEXT, topics_json TEXT,
    page_start INTEGER, page_end INTEGER, structured_by TEXT
);
CREATE TABLE sections(
    section_id TEXT PRIMARY KEY, chapter_id TEXT, ord INTEGER, heading TEXT, text TEXT,
    page_start INTEGER, page_end INTEGER
);
CREATE TABLE key_terms(chapter_id TEXT, term TEXT, meaning TEXT, page INTEGER);
CREATE TABLE activities(chapter_id TEXT, ord INTEGER, label TEXT, description TEXT, page INTEGER);
CREATE TABLE figures(chapter_id TEXT, ord INTEGER, label TEXT, caption TEXT, page INTEGER);
CREATE TABLE exercises(chapter_id TEXT, ord INTEGER, label TEXT, text TEXT, type_hint TEXT, page INTEGER, source TEXT);
CREATE TABLE old_map(old_title TEXT, old_class INTEGER, old_subject TEXT, old_book TEXT, new_chapter_id TEXT, note TEXT);
CREATE INDEX idx_sections_ch ON sections(chapter_id, ord);
CREATE INDEX idx_chapters_cs ON chapters(class, subject, chapter_no);
CREATE VIRTUAL TABLE fts_sections USING fts5(
    text, heading, chapter_id UNINDEXED, section_id UNINDEXED, tokenize='porter unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE fts_chapters USING fts5(
    title, topics, summary, chapter_id UNINDEXED, tokenize='porter unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE fts_extras USING fts5(
    text, kind UNINDEXED, chapter_id UNINDEXED, page UNINDEXED, tokenize='porter unicode61 remove_diacritics 2'
);
"""


def load_chapters(folder: Path) -> list[Chapter]:
    chapters = []
    for path in sorted(folder.glob("*.json")):
        chapters.append(clean_chapter(Chapter.model_validate_json(path.read_text(encoding="utf-8"))))
    return chapters


def load_old_map(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for item in data.get("entries", data) if isinstance(data, dict) else data:
        targets = item.get("new_chapter_ids") or [None]  # empty list = not in the new syllabus
        for target in targets:
            rows.append(
                (
                    item["old_title"], item.get("old_class"), item.get("old_subject"), item.get("old_book"),
                    target, item.get("note", ""),
                )
            )
    return rows


def build(chapters: list[Chapter], old_map_rows: list[tuple], out: Path) -> None:
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    ids = {c.chapter_id for c in chapters}
    for c in chapters:
        db.execute(
            "INSERT INTO chapters VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.chapter_id, c.class_, c.subject, c.book, c.part, c.chapter_no, c.title, c.summary,
             json.dumps(c.topics, ensure_ascii=False), c.page_start, c.page_end, c.structured_by),
        )
        db.execute(
            "INSERT INTO fts_chapters VALUES (?,?,?,?)", (c.title, " ; ".join(c.topics), c.summary, c.chapter_id)
        )
        for i, s in enumerate(c.sections):
            db.execute(
                "INSERT INTO sections VALUES (?,?,?,?,?,?,?)",
                (s.section_id, c.chapter_id, i, s.heading, s.text, s.page_start, s.page_end),
            )
            plain = re.sub(r"<!-- (?:answers )?page \d+ -->", " ", s.text)  # page markers are not words to search
            db.execute("INSERT INTO fts_sections VALUES (?,?,?,?)", (plain, s.heading, c.chapter_id, s.section_id))
        for k in c.key_terms:
            db.execute("INSERT INTO key_terms VALUES (?,?,?,?)", (c.chapter_id, k.term, k.meaning, k.page))
            db.execute("INSERT INTO fts_extras VALUES (?,?,?,?)", (f"{k.term}: {k.meaning}", "key_term", c.chapter_id, k.page))
        for i, a in enumerate(c.activities):
            db.execute("INSERT INTO activities VALUES (?,?,?,?,?)", (c.chapter_id, i, a.label, a.description, a.page))
        for i, f in enumerate(c.figures):
            db.execute("INSERT INTO figures VALUES (?,?,?,?,?)", (c.chapter_id, i, f.label, f.caption, f.page))
        for i, e in enumerate(c.exercises):
            db.execute(
                "INSERT INTO exercises VALUES (?,?,?,?,?,?,?)",
                (c.chapter_id, i, e.label, e.text, e.type_hint, e.page, e.source),
            )
            db.execute("INSERT INTO fts_extras VALUES (?,?,?,?)", (e.text, "exercise", c.chapter_id, e.page))
    bad_targets = [r for r in old_map_rows if r[4] and r[4] not in ids]
    if bad_targets and ids:
        print(f"WARNING: old map points at chapters that don't exist: {sorted({r[4] for r in bad_targets})}")
    db.executemany("INSERT INTO old_map VALUES (?,?,?,?,?,?)", old_map_rows)
    db.execute("INSERT INTO meta VALUES ('built_at', ?)", (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    db.execute("INSERT INTO meta VALUES ('chapters', ?)", (str(len(chapters)),))
    db.commit()
    db.execute("VACUUM")
    db.close()
    os.replace(tmp, out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--structured", type=Path, default=STRUCTURED)
    ap.add_argument("--old-map", type=Path, default=OLD_MAP)
    ap.add_argument("--out", type=Path, default=BOOKS_DB)
    args = ap.parse_args()
    chapters = load_chapters(args.structured)
    if not chapters:
        print(f"No chapter JSON in {args.structured}. Run ingest.structure first.")
        return 1
    rows = load_old_map(args.old_map)
    build(chapters, rows, args.out)
    by = {}
    for c in chapters:
        by.setdefault((c.class_, c.subject), []).append(c.chapter_no)
    print(f"Wrote {args.out} ({args.out.stat().st_size // 1024} KB): {len(chapters)} chapters, {len(rows)} old-map rows")
    for (cls, subj), nos in sorted(by.items()):
        print(f"  class {cls} {subj}: chapters {sorted(nos)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
