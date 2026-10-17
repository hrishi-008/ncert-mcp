"""Teacher notes + paper log, stored in state.db.

Everything that touches state.db lives in this one file, so swapping SQLite for Turso (or anything
else) later only means rewriting this class. Nothing is ever hard-deleted: deleting a note marks it
inactive, and every change writes a notes_history row.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

NOTE_SECTIONS = {
    "paper_preferences": "Paper preferences",
    "question_types": "Question types",
    "format_layout": "Format and layout",
    "corrections": "Corrections",
}
_SECTION_ALIASES = {
    "preferences": "paper_preferences", "paper": "paper_preferences", "paper_preference": "paper_preferences",
    "questions": "question_types", "question_type": "question_types", "types": "question_types",
    "format": "format_layout", "layout": "format_layout", "formatting": "format_layout",
    "correction": "corrections", "mistakes": "corrections", "fixes": "corrections",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(
    note_id INTEGER PRIMARY KEY AUTOINCREMENT, section TEXT NOT NULL, text TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER NOT NULL, action TEXT NOT NULL,
    old_text TEXT, new_text TEXT, reason TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS papers(
    paper_id INTEGER PRIMARY KEY AUTOINCREMENT, class INTEGER, subject TEXT, chapter_ids_json TEXT,
    total_marks REAL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_questions(
    paper_id INTEGER NOT NULL, ord INTEGER, section TEXT, type TEXT, marks REAL, text TEXT
);
CREATE INDEX IF NOT EXISTS idx_pq_paper ON paper_questions(paper_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_section(section: str) -> str:
    key = str(section).strip().lower().replace(" ", "_").replace("-", "_")
    key = _SECTION_ALIASES.get(key, key)
    if key not in NOTE_SECTIONS:
        raise ValueError(f"Unknown notes section {section!r}. Use one of: {', '.join(NOTE_SECTIONS)}.")
    return key


def path_from_url(url: str) -> Path:
    """'sqlite:////var/lib/ncert/state.db', 'sqlite:///data/state.db' or a plain path -> Path."""
    if url.startswith("sqlite:///"):
        return Path(url[len("sqlite:///"):])
    if "://" in url:
        raise ValueError(f"Only sqlite state DBs are supported for now, got {url!r}")
    return Path(url)


class State:
    def __init__(self, url: str):
        self.path = path_from_url(url)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def _tx(self):
        return _Tx(self._conn, self._lock)

    def ok(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1").fetchone()[0] == 1

    # -- notes --------------------------------------------------------------------------------
    def notes(self, include_inactive: bool = False) -> list[dict]:
        q = "SELECT * FROM notes" + ("" if include_inactive else " WHERE active=1") + " ORDER BY section, note_id"
        with self._lock:
            return [dict(r) for r in self._conn.execute(q)]

    def notes_markdown(self) -> str:
        notes = self.notes()
        if not notes:
            return "_No teacher notes saved yet._"
        out = []
        for key, title in NOTE_SECTIONS.items():
            items = [n for n in notes if n["section"] == key]
            if items:
                out.append(f"### {title}")
                out += [f"- [note {n['note_id']}] {n['text']}" for n in items]
                out.append("")
        return "\n".join(out).strip()

    def update_note(self, section: str, action: str, text: str | None, reason: str, note_id: int | None = None) -> dict:
        action = str(action).strip().lower()
        if action in ("edit", "update", "change", "modify"):
            action = "replace"
        if action in ("remove", "drop"):
            action = "delete"
        if action not in ("add", "replace", "delete"):
            raise ValueError("action must be 'add', 'replace' or 'delete'.")
        sec = normalize_section(section)
        text = (text or "").strip()
        reason = (reason or "").strip() or "(no reason given)"
        if action == "add":
            if not text:
                raise ValueError("To add a note, give its text.")
            with self._tx() as c:
                dup = c.execute(
                    "SELECT note_id FROM notes WHERE active=1 AND section=? AND lower(trim(text))=lower(?)", (sec, text)
                ).fetchone()
                if dup:
                    return {"status": "already_saved", "note_id": dup["note_id"], "section": sec}
                t = now()
                nid = c.execute(
                    "INSERT INTO notes(section, text, active, created_at, updated_at) VALUES (?,?,1,?,?)", (sec, text, t, t)
                ).lastrowid
                c.execute(
                    "INSERT INTO notes_history(note_id, action, old_text, new_text, reason, at) VALUES (?,?,?,?,?,?)",
                    (nid, "add", None, text, reason, t),
                )
            return {"status": "added", "note_id": nid, "section": sec}

        if note_id in (None, ""):
            raise ValueError(f"To {action} a note, give its note_id (shown as [note N] in get_teacher_notes).")
        note_id = int(note_id)
        with self._tx() as c:
            row = c.execute("SELECT * FROM notes WHERE note_id=?", (note_id,)).fetchone()
            if row is None or not row["active"]:
                raise ValueError(f"There is no current note {note_id}. Call get_teacher_notes to see note ids.")
            t = now()
            if action == "replace":
                if not text:
                    raise ValueError("To replace a note, give the new text.")
                c.execute("UPDATE notes SET text=?, section=?, updated_at=? WHERE note_id=?", (text, sec, t, note_id))
                c.execute(
                    "INSERT INTO notes_history(note_id, action, old_text, new_text, reason, at) VALUES (?,?,?,?,?,?)",
                    (note_id, "replace", row["text"], text, reason, t),
                )
                return {"status": "replaced", "note_id": note_id, "section": sec}
            c.execute("UPDATE notes SET active=0, updated_at=? WHERE note_id=?", (t, note_id))
            c.execute(
                "INSERT INTO notes_history(note_id, action, old_text, new_text, reason, at) VALUES (?,?,?,?,?,?)",
                (note_id, "delete", row["text"], None, reason, t),
            )
            return {"status": "deleted", "note_id": note_id, "section": row["section"]}

    def history(self, note_id: int | None = None) -> list[dict]:
        q, args = "SELECT * FROM notes_history", []
        if note_id is not None:
            q += " WHERE note_id=?"
            args.append(note_id)
        with self._lock:
            return [dict(r) for r in self._conn.execute(q + " ORDER BY id", args)]

    def restore(self, history_id: int, reason: str = "restored by admin") -> dict:
        """Admin only (admin/notes_cli.py): bring a note back to how it was at a history row."""
        with self._tx() as c:
            h = c.execute("SELECT * FROM notes_history WHERE id=?", (history_id,)).fetchone()
            if h is None:
                raise ValueError(f"No history row {history_id}")
            # The text the note had right after this change (or before it, for a delete).
            text = h["new_text"] if h["action"] != "delete" else h["old_text"]
            row = c.execute("SELECT * FROM notes WHERE note_id=?", (h["note_id"],)).fetchone()
            t = now()
            c.execute("UPDATE notes SET text=?, active=1, updated_at=? WHERE note_id=?", (text, t, h["note_id"]))
            c.execute(
                "INSERT INTO notes_history(note_id, action, old_text, new_text, reason, at) VALUES (?,?,?,?,?,?)",
                (h["note_id"], "restore", row["text"] if row["active"] else None, text, reason, t),
            )
        return {"status": "restored", "note_id": h["note_id"], "text": text}

    # -- paper log ----------------------------------------------------------------------------
    def log_paper(self, cls: int, subject: str, chapter_ids: list[str], total_marks: float, questions: list[dict]) -> int:
        with self._tx() as c:
            pid = c.execute(
                "INSERT INTO papers(class, subject, chapter_ids_json, total_marks, created_at) VALUES (?,?,?,?,?)",
                (cls, subject, json.dumps(chapter_ids), total_marks, now()),
            ).lastrowid
            c.executemany(
                "INSERT INTO paper_questions(paper_id, ord, section, type, marks, text) VALUES (?,?,?,?,?,?)",
                [(pid, i, q.get("section", ""), q.get("type", ""), q.get("marks"), q.get("text", "")) for i, q in enumerate(questions)],
            )
        return pid

    def past_questions(self, chapter_id: str, last_n_papers: int = 5) -> list[dict]:
        with self._lock:
            papers = [
                dict(r) for r in self._conn.execute(
                    "SELECT p.* FROM papers p, json_each(p.chapter_ids_json) j WHERE j.value=? "
                    "ORDER BY p.paper_id DESC LIMIT ?",
                    (chapter_id, max(1, int(last_n_papers))),
                )
            ]
            for p in papers:
                p["chapter_ids"] = json.loads(p.pop("chapter_ids_json"))
                p["questions"] = [
                    dict(r) for r in self._conn.execute(
                        "SELECT section, type, marks, text FROM paper_questions WHERE paper_id=? ORDER BY ord", (p["paper_id"],)
                    )
                ]
        return papers

    def papers(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM papers ORDER BY paper_id DESC LIMIT ?", (limit,))]

    def close(self) -> None:
        self._conn.close()


class _Tx:
    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock):
        self.conn, self.lock = conn, lock

    def __enter__(self) -> sqlite3.Connection:
        self.lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.lock.release()
