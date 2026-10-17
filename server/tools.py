"""The 9 MCP tools. Descriptions are written as instructions to Claude: when to use, what comes back.

Tools return Markdown text (small, easy for Claude to read). Expected problems (bad input,
unknown chapter) come back as a clear error message with suggestions, never a bare "not found".
"""

from __future__ import annotations

import functools
import logging
import time
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from server.books import Books, BooksUnavailable, strip_md
from server.normalize import SUBJECT_NAMES, NormalizeError, normalize_class, normalize_subject
from server.state import NOTE_SECTIONS, State

log = logging.getLogger("ncert.tools")
PACK_CHAR_BUDGET = 30_000  # ~8k tokens

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


class PaperQuestion(BaseModel):
    section: str = Field("", description="Paper section, e.g. 'A' or 'Section B: Short answers'")
    type: str = Field("", description="mcq / fill_blank / true_false / match / short / long / numerical / diagram / other")
    marks: float | None = Field(None, description="Marks for this question")
    text: str = Field(..., description="The full question text as printed on the paper")


def _guard(fn):
    """Log tool name + duration + errors (never content), and turn expected errors into ToolError."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        status = "ok"
        try:
            return fn(*args, **kwargs)
        except (NormalizeError, LookupError, ValueError, BooksUnavailable) as e:
            status = f"user_error:{type(e).__name__}"
            raise ToolError(str(e)) from e
        except ToolError:
            status = "tool_error"
            raise
        except Exception:
            status = "crash"
            log.exception("tool %s crashed", fn.__name__)
            raise ToolError("Something went wrong on the server. Try once more; if it keeps failing, tell the teacher.")
        finally:
            log.info("tool=%s status=%s ms=%.0f", fn.__name__, status, (time.perf_counter() - start) * 1000)

    return wrapper


def register(mcp: Any, books: Books, state: State) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @_guard
    def find_chapter(
        query: Annotated[str, Field(description="Anything the teacher said about the chapter: 'ch 5', 'VII sci ch5', a title (typos ok), a nickname like 'the plants chapter', an OLD NCERT chapter name like 'Nutrition in Plants', or a topic like 'photosynthesis'.")],
        class_: Annotated[str | int | None, Field(description="Class if known: 6 or 7 (also accepts 'VII', 'class 7', 'std 6').")] = None,
        subject: Annotated[str | None, Field(description="Subject if known: science/sci/Curiosity, maths/Ganita Prakash, sst/social science/history/geography/civics.")] = None,
    ) -> str:
        """Find the right chapter in the NEW NCERT books from whatever the teacher says.

        Use this FIRST whenever a request names a chapter, a chapter number, a topic, or an old chapter
        name. Chapter names and numbers changed in the new books, so never guess a chapter from memory.
        Returns up to 3 matches with chapter_id, title, why it matched and a confidence (high/medium/low).
        If a match came from an old chapter name, it includes a 'Tell the teacher' line: pass it on.
        If confidence is low or there are no matches, tell her the topic may not be in the new syllabus
        (or ask which class/subject she means) instead of guessing.
        """
        res = books.find_chapter(query, class_, subject)
        lines = []
        if not res["matches"]:
            lines.append("No matching chapter found.")
        for m in res["matches"]:
            if m.get("chapter_id"):
                lines.append(
                    f"- **{m['chapter_id']}**: Class {m['class']} {SUBJECT_NAMES[m['subject']]}, "
                    f"Ch {m['chapter_no']} '{m['title']}'" + (f" (Part {m['part']})" if m.get("part", 1) > 1 else "")
                    + f" | confidence: {m['confidence']} | {m['match_reason']}"
                )
            else:
                lines.append(f"- (no new chapter) | confidence: {m['confidence']} | {m['match_reason']}")
            if m.get("tell_teacher"):
                lines.append(f"  Tell the teacher: {m['tell_teacher']}")
        for n in res["notes"]:
            lines.append(f"\nNote: {n}")
        return "\n".join(lines)

    @mcp.tool(annotations=READ_ONLY)
    @_guard
    def list_chapters(
        class_: Annotated[str | int, Field(description="6 or 7 (also 'VII', 'class 7', 'std 6')")],
        subject: Annotated[str, Field(description="science / maths / sst (or book names: Curiosity, Ganita Prakash, Exploring Society)")],
    ) -> str:
        """List every chapter of one book (new NCERT): number, title and a one-line summary.

        Use when the teacher asks what chapters there are, or to show her the choices. Part 1 and
        Part 2 are one book here, with chapter numbers running on (the way students count them).
        """
        res = books.list_chapters(class_, subject)
        lines = [f"Class {res['class']} {SUBJECT_NAMES[res['subject']]}: *{res['book']}*", ""]
        part = None
        for c in res["chapters"]:
            if c["part"] != part and any(x["part"] == 2 for x in res["chapters"]):
                part = c["part"]
                lines.append(f"Part {part}:")
            lines.append(f"{c['chapter_no']}. {c['title']} ({c['chapter_id']})" + (f": {c['one_line']}" if c["one_line"] else ""))
        return "\n".join(lines)

    @mcp.tool(annotations=READ_ONLY)
    @_guard
    def get_chapter_pack(
        chapter_id: Annotated[str, Field(description="Chapter id from find_chapter, e.g. '7-sci-05'")],
    ) -> str:
        """The main tool: everything needed to write a paper on one chapter, in one call.

        Call it once per chapter before writing questions. Returns the summary, every section heading
        with a short preview, key terms, activities, figures, the book's own exercises, AND the
        teacher's saved notes (her preferences and past corrections: follow them). Questions must only
        test what this chapter teaches. For the full text of a section, call get_section.
        """
        p = books.chapter_pack(chapter_id)
        notes = state.notes_markdown()
        return _format_pack(p, notes)

    @mcp.tool(annotations=READ_ONLY)
    @_guard
    def get_section(
        chapter_id: Annotated[str, Field(description="Chapter id, e.g. '7-sci-05'")],
        section_id: Annotated[str, Field(description="Section id from get_chapter_pack (e.g. '7-sci-05-s03'); a section number or heading words also work")],
    ) -> str:
        """Full text of one section of a chapter, exactly as in the book, with page markers.

        Use when you need exact facts, definitions, values or wording for questions and answer keys.
        """
        s = books.get_section(chapter_id, section_id)
        return (
            f"## {s['heading']}\n{s['chapter_id']} '{s['title']}', section {s['section_id']}, pages {s['pages']}\n\n{s['text']}"
        )

    @mcp.tool(annotations=READ_ONLY)
    @_guard
    def search_books(
        query: Annotated[str, Field(description="Words to look for, e.g. 'photosynthesis', 'Harappan drains', 'latitude'")],
        class_: Annotated[str | int | None, Field(description="Optional: 6 or 7")] = None,
        subject: Annotated[str | None, Field(description="Optional: science / maths / sst")] = None,
        chapter_id: Annotated[str | None, Field(description="Optional: search inside one chapter only")] = None,
        limit: Annotated[int, Field(description="How many results (1-25)", ge=1, le=25)] = 8,
    ) -> str:
        """Search the text of the new NCERT books. Returns snippets with chapter, section and page.

        Use to check whether a topic or fact is in the syllabus, or to find exact wording. If it says
        nothing was found, tell the teacher it isn't in the book. Do not make up content.
        """
        res = books.search(query, class_, subject, chapter_id, limit)
        if not res["results"]:
            return f"Nothing found for '{query}' in the new NCERT books (with these filters). It is probably not in the book."
        lines = []
        if res["matched"] == "some words":
            lines.append("No section has all the words; these have some of them:\n")
        for r in res["results"]:
            lines.append(f"- {r['chapter_id']} '{r['chapter_title']}' > {strip_md(r['heading'])} ({r['section_id']}, p. {r['page']}): {r['snippet']}")
        return "\n".join(lines)

    @mcp.tool(annotations=READ_ONLY)
    @_guard
    def get_teacher_notes() -> str:
        """The teacher's saved notes: her paper preferences, question types, format/layout, and corrections.

        They are already included in get_chapter_pack. Call this on its own when no chapter is involved,
        or before updating a note (to get its note id).
        """
        return state.notes_markdown()

    @mcp.tool(annotations=WRITE)
    @_guard
    def update_teacher_notes(
        section: Annotated[str, Field(description="One of: paper_preferences, question_types, format_layout, corrections")],
        action: Annotated[str, Field(description="add | replace | delete (replace and delete need note_id)")],
        text: Annotated[str, Field(description="The note, written as a short clear rule, e.g. 'No true/false questions'. Empty for delete.")] = "",
        reason: Annotated[str, Field(description="Why, in a few words, e.g. 'She removed true/false twice'")] = "",
        note_id: Annotated[int | None, Field(description="For replace/delete: the id shown as [note N]")] = None,
    ) -> str:
        """Save what you learn about the teacher's style so you don't repeat mistakes.

        Use when she corrects you, states a preference ('always 2 marks for short answers'), or keeps
        changing the same thing. Keep notes short and general. Replace a note instead of adding a
        conflicting one. Every change is kept in history; nothing is lost.
        """
        res = state.update_note(section, action, text, reason, note_id)
        if res["status"] == "already_saved":
            return f"That note is already saved as note {res['note_id']} ({res['section']}). Nothing changed."
        return f"Note {res['note_id']} {res['status']} in {NOTE_SECTIONS[res['section']]}."

    @mcp.tool(annotations=WRITE)
    @_guard
    def log_paper(
        class_: Annotated[str | int, Field(description="6 or 7")],
        subject: Annotated[str, Field(description="science / maths / sst")],
        chapter_ids: Annotated[list[str], Field(description="Chapter ids the paper covers, e.g. ['7-sci-05']")],
        total_marks: Annotated[float, Field(description="Total marks of the paper")],
        questions: Annotated[list[PaperQuestion], Field(description="Every question on the final paper")],
    ) -> str:
        """Save a finished paper. Call once after every paper the teacher is happy with (the final version)."""
        cls, subj = normalize_class(class_), normalize_subject(subject)
        ids = [books.chapter(c)["chapter_id"] for c in chapter_ids] if books.available() else list(chapter_ids)
        if not questions:
            raise ValueError("The paper has no questions; send every question on the final paper.")
        qs = [q.model_dump() if isinstance(q, BaseModel) else dict(q) for q in questions]
        pid = state.log_paper(cls, subj, ids, total_marks, qs)
        return f"Saved as paper {pid}: Class {cls} {SUBJECT_NAMES[subj]}, {', '.join(ids)}, {total_marks:g} marks, {len(qs)} questions."

    @mcp.tool(annotations=READ_ONLY)
    @_guard
    def get_past_questions(
        chapter_id: Annotated[str, Field(description="Chapter id, e.g. '7-sci-05'")],
        last_n_papers: Annotated[int, Field(description="How many recent papers to look at", ge=1, le=50)] = 5,
    ) -> str:
        """Questions used in earlier papers on a chapter.

        Use ONLY when the teacher asks to avoid repeating earlier questions. Repeats are fine otherwise,
        so don't call this by default.
        """
        cid = books.chapter(chapter_id)["chapter_id"] if books.available() else chapter_id
        papers = state.past_questions(cid, last_n_papers)
        if not papers:
            return f"No saved papers include {cid} yet."
        lines = []
        for p in papers:
            lines.append(f"### Paper {p['paper_id']} ({p['created_at'][:10]}, {p['total_marks']:g} marks, chapters {', '.join(p['chapter_ids'])})")
            for q in p["questions"]:
                lines.append(f"- [{q['type'] or '?'}, {q['marks'] if q['marks'] is not None else '?'} marks] {q['text']}")
        return "\n".join(lines)


def _format_pack(p: dict, notes: str) -> str:
    def build(preview_len: int | None, desc_len: int | None, max_figs: int | None) -> str:
        out = [
            f"# {p['chapter_id']}: Class {p['class']} {SUBJECT_NAMES[p['subject']]}, Ch {p['chapter_no']} '{p['title']}'",
            f"Book: {p['book']}" + (f" (Part {p['part']})" if p["part"] > 1 else "") + f" | pages {p['pages']}",
        ]
        if p["structured_by"] == "heuristic":
            out.append("(Structure was made automatically without an AI pass; headings and exercises may be rough. Use get_section for exact text.)")
        if p["summary"]:
            out += ["", "## Summary", p["summary"]]
        if p["topics"]:
            out += ["", "Topics: " + ", ".join(p["topics"])]
        out += ["", "## Sections (call get_section for full text)"]
        for s in p["sections"]:
            prev = s["preview"] if preview_len is None else s["preview"][:preview_len]
            out.append(f"- {s['section_id']} | {strip_md(s['heading'])} (p. {s['pages']})" + (f": {prev}" if prev else ""))
        if p["key_terms"]:
            out += ["", "## Key terms"]
            out += [f"- {k['term']}" + (f": {k['meaning']}" if k["meaning"] else "") + (f" (p. {k['page']})" if k["page"] else "") for k in p["key_terms"]]
        if p["activities"]:
            out += ["", "## Activities"]
            for a in p["activities"]:
                d = a["description"] if desc_len is None else a["description"][:desc_len]
                out.append(f"- {a['label']}" + (f": {d}" if d else "") + (f" (p. {a['page']})" if a["page"] else ""))
        if p["figures"]:
            figs = p["figures"] if max_figs is None else p["figures"][:max_figs]
            out += ["", "## Figures"]
            out += [f"- {f['label']}: {f['caption']}" + (f" (p. {f['page']})" if f["page"] else "") for f in figs]
            if len(figs) < len(p["figures"]):
                out.append(f"- … {len(p['figures']) - len(figs)} more figures")
        if p["exercises"]:
            out += ["", "## Exercises in the book"]
            for e in p["exercises"]:
                hint = f" [{e['type_hint']}]" if e["type_hint"] else ""
                where = " (in-chapter box)" if e["source"] == "in_chapter" else ""
                out.append(f"- {e['label']}{hint}{where}: {e['text']}")
        out += ["", "## Teacher notes (follow these)", notes]
        return "\n".join(out)

    for args in [(None, None, None), (160, 200, None), (100, 120, 25), (60, 60, 10), (0, 0, 0)]:
        text = build(*args)
        if len(text) <= PACK_CHAR_BUDGET:
            return text
    return text[:PACK_CHAR_BUDGET] + "\n…(cut to keep the response small; use get_section / search_books for more)"
