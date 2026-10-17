"""Stage 2: chapter Markdown -> structured chapter JSON (data/structured/<id>.json).

Book text is NEVER rewritten. In both modes the text of sections, exercises and activities is
sliced out of the extracted Markdown by line number, so it is copied by construction; a substring
check then confirms it. Only labels, the summary and topic tags are written by the LLM.

Modes:
  --mode heuristic   no API needed: rules + data/book_config.yaml (headings, exercises, bold terms)
  --mode llm         Claude reads the numbered lines and returns line numbers + labels (Claude Code login or API key)
  default: heuristic (pass --mode llm to use Claude)

Usage:
    python -m ingest.structure 7-sci-05
    python -m ingest.structure --all [--mode heuristic|llm] [--force]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import pymupdf
import yaml

from ingest.common import ANSWERS_HEADING, DATA, EXTRACTED, STRUCTURED, clean_text, load_book_config, load_manifest, select, squash
from ingest.glyphs import GlyphRepair
from ingest.schema import Activity, Chapter, Exercise, Figure, KeyTerm, Section

PAGE_RE = re.compile(r"<!-- page (\d+) -->")
HEAD_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
NUMBERED_RE = re.compile(r"^\d{1,2}(\.\d{1,2})+\.?\s+\S")
FIG_RE = re.compile(r"^(?:>\s*)?(?:\[Figure:\s*)?(Fig\.?|Figure)\s*(\d+(?:\.\d+)*[a-z]?)\s*[:.]?\s*(.*?)\]?$", re.I)
ITEM_RE = re.compile(r"^(?:>\s*)?(?:Q\.?\s*)?(\d{1,2})[.)]\s+\S")
STRUCTURE_VERSION = "1"
# Bold action words in activity steps (and the keyword box's skills column) are not subject terms.
SKILL_VERBS = {
    "observe", "record", "create", "group", "explore", "analyse", "analyze", "compare", "relate", "discuss",
    "predict", "draw", "measure", "classify", "identify", "list", "note", "collect", "think", "find", "investigate",
    "communicate", "evaluate", "apply", "test", "infer", "reflect", "design", "plan", "conclude", "experiment",
    "interpret", "question", "appreciate", "imagine", "share", "construct", "estimate", "reason", "visualise",
}


def key(text: str) -> str:
    """Comparison form for headings: lower case, letters only ('S u m m a r y' -> 'summary')."""
    return re.sub(r"[^a-z]", "", clean_text(text).lower().replace("’", "'"))


ANY_PAGE_RE = re.compile(r"<!-- (?:answers )?page \d+ -->")


def strip_md(text: str) -> str:
    text = ANY_PAGE_RE.sub(" ", text)
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"^#+\s*|^>\s*", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


class Doc:
    """The extracted Markdown as lines, with the printed page of every line."""

    def __init__(self, markdown: str):
        self.markdown = markdown
        self.lines = markdown.split("\n")
        self.page: list[int | None] = []
        cur = None
        for line in self.lines:
            m = PAGE_RE.fullmatch(line.strip())
            if m:
                cur = int(m.group(1))
            self.page.append(cur)
        # Lines before the first marker belong to the first page.
        first = next((p for p in self.page if p is not None), None)
        self.page = [p if p is not None else first for p in self.page]

    def heading(self, i: int) -> tuple[int, str] | None:
        m = HEAD_RE.match(self.lines[i])
        return (len(m.group(1)), m.group(2).strip()) if m else None

    def text(self, a: int, b: int) -> str:
        return "\n".join(self.lines[a:b]).strip()

    def page_range(self, a: int, b: int) -> tuple[int | None, int | None]:
        # A section that starts with a page marker starts on that page.
        start = a
        while start < b and not self.lines[start].strip():
            start += 1
        return self.page[min(start, len(self.page) - 1)], self.page[max(a, b - 1)]

    def numbered(self) -> str:
        return "\n".join(f"{i}: {line}" for i, line in enumerate(self.lines))


def matches(text: str, names: list[str]) -> bool:
    k = key(text)
    return any(k == key(n) or (len(key(n)) > 5 and k.startswith(key(n))) for n in names)


# ---------------------------------------------------------------------------------------------
# Shared builders (both modes): everything is cut from the Markdown by line number.
# ---------------------------------------------------------------------------------------------

def build_sections(doc: Doc, cid: str, starts: list[tuple[int, str]]) -> list[Section]:
    starts = sorted({i: h for i, h in starts if 0 <= i < len(doc.lines)}.items())
    if not starts or starts[0][0] > 0:
        first = doc.heading(0)
        starts.insert(0, (0, first[1] if first else "Introduction"))
    # Merge sections that are nearly empty (e.g. a heading split over two lines) into the next one.
    merged: list[tuple[int, str]] = []
    for n, (i, h) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(doc.lines)
        body = strip_md(doc.text(i, end))
        if merged and len(body) - len(strip_md(h)) < 40 and n + 1 < len(starts):
            continue  # its lines are kept: they fall inside the previous section's slice
        merged.append((i, h))
    # A first "section" that is only the chapter title line joins the section after it.
    if len(merged) > 1 and len(strip_md(doc.text(merged[0][0], merged[1][0]))) - len(strip_md(merged[0][1])) < 40:
        merged = [(merged[0][0], merged[1][1])] + merged[2:]
    sections = []
    for n, (i, h) in enumerate(merged):
        end = merged[n + 1][0] if n + 1 < len(merged) else len(doc.lines)
        a, b = doc.page_range(i, end)
        sections.append(Section(section_id=f"{cid}-s{n + 1:02d}", heading=strip_md(h), text=doc.text(i, end),
                                page_start=a, page_end=b))
    return sections


def type_hint(text: str) -> str:
    low = text.lower()
    if re.search(r"\(a\).*\(b\).*\(c\)", low, re.S) and re.search(r"\(d\)|choose|correct|which of", low):
        return "mcq"
    if "true or false" in low or "true/false" in low or re.search(r"\b(t|f)\s*/\s*(f|t)\b", low):
        return "true_false"
    if "____" in text or "fill in" in low or re.search(r"_{3,}", text):
        return "fill_blank"
    if "match" in low and ("column" in low or "following" in low):
        return "match"
    if re.search(r"\b(draw|label|sketch|diagram)\b", low):
        return "diagram"
    if re.search(r"\b(find|calculate|how many|how much|what is the value|solve|evaluate)\b", low) and \
            re.search(r"\d\s*[-+×x÷/=%:]\s*\d|\d\.\d|₹|\$", text):
        return "numerical"
    if re.search(r"\b(explain|describe|discuss|why|justify|give reasons)\b", low):
        return "long"
    return "short"


def split_items(doc: Doc, a: int, b: int, label: str, source: str) -> list[Exercise]:
    """Split the lines a..b of an exercise block into numbered questions (text copied verbatim)."""
    starts = [i for i in range(a, b) if ITEM_RE.match(doc.lines[i].strip())]
    out = []
    if not starts:
        text = doc.text(a, b)
        if strip_md(text):
            out.append(Exercise(label=label, text=text, type_hint=type_hint(text), page=doc.page[a], source=source))
        return out
    lead = doc.text(a, starts[0])  # instructions before Q1 ("Answer the following") belong to Q1's context
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else b
        text = doc.text(i, end)
        num = ITEM_RE.match(doc.lines[i].strip()).group(1)
        if n == 0 and lead and len(strip_md(lead)) < 300:
            text = lead + "\n" + text
        out.append(Exercise(label=f"{label} Q{num}", text=text, type_hint=type_hint(text), page=doc.page[i], source=source))
    return out


def find_glossary(entry: dict) -> dict[str, str]:
    """Glossary PDF (SST) -> {term: meaning}."""
    extras_file = DATA / "extras.yaml"
    if not extras_file.exists():
        return {}
    out: dict[str, str] = {}
    for x in yaml.safe_load(extras_file.read_text()) or []:
        if x["kind"] != "glossary" or x["class"] != entry["class"] or x["subject"] != entry["subject"]:
            continue
        path = DATA / x["file"]
        if not path.exists():
            continue
        doc = pymupdf.open(path)
        repair = GlyphRepair(doc)
        for page in doc:
            blocks = page.get_text("rawdict")["blocks"]
            repair.fix_page(page, blocks)
            for b in blocks:
                spans = [s for line in b.get("lines", []) for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                bold = [s for s in spans if s["flags"] & 16 or "bold" in s["font"].lower()]
                if bold and spans[0] in bold:
                    term = clean_text("".join(s["text"] for s in spans if s in bold)).strip(" :–—-")
                    meaning = clean_text("".join(s["text"] for s in spans if s not in bold)).strip(" :–—-")
                    if 2 <= len(term) <= 60 and meaning:
                        out[term] = re.sub(r"\s+", " ", meaning)
    return out


def key_terms(doc: Doc, sections: list[Section], keyword_names: list[str], glossary: dict[str, str]) -> list[KeyTerm]:
    terms: dict[str, KeyTerm] = {}
    # 1. Bold words in running text, with the sentence they are defined in (copied verbatim).
    for i, line in enumerate(doc.lines):
        if HEAD_RE.match(line) or line.startswith("|"):
            continue
        for m in re.finditer(r"\*\*(.+?)\*\*", line):
            term = m.group(1).strip(" .,:;")
            if term.count("(") > term.count(")"):
                term += ")"
            term = term.strip("() ") if term.startswith("(") and term.endswith(")") else term
            if not (2 < len(term) <= 60) or term.lower() in terms or not re.search(r"[A-Za-z]{3}", term):
                continue
            if term.lower() in SKILL_VERBS:
                continue  # "**Observe**", "**Record**": activity instructions, not terms
            plain = strip_md(line)
            sentences = re.split(r"(?<=[.!?])\s+", plain)
            sent = next((s for s in sentences if term.lower() in s.lower()), plain)
            terms[term.lower()] = KeyTerm(term=term, meaning=sent[:300], page=doc.page[i])
    # 2. The chapter's own keyword box.
    for s in sections:
        if matches(s.heading, keyword_names):
            body = "\n".join(l for l in s.text.split("\n")[1:] if not PAGE_RE.fullmatch(l.strip()))
            for cell in re.split(r"\s*\|\s*|\n", body):
                t = strip_md(cell).strip(" .,")
                if 2 < len(t) <= 60 and t.lower() not in terms and t.lower() not in SKILL_VERBS:
                    terms[t.lower()] = KeyTerm(term=t, meaning="", page=s.page_start)
    # 3. Book glossary meanings for terms used in this chapter.
    text_low = " " + squash(doc.markdown) + " "
    for term, meaning in glossary.items():
        if f" {squash(term)} " in text_low:
            k = term.lower()
            if k in terms and not terms[k].meaning:
                terms[k].meaning = f"(glossary) {meaning}"
            elif k not in terms:
                terms[k] = KeyTerm(term=term, meaning=f"(glossary) {meaning}", page=None)
    return list(terms.values())


def figures(doc: Doc) -> list[Figure]:
    out, seen = [], set()
    for i, line in enumerate(doc.lines):
        m = FIG_RE.match(line.strip())
        if not m or "figure it out" in line.lower():
            continue
        label = f"Fig. {m.group(2)}"
        if label in seen:
            continue
        seen.add(label)
        out.append(Figure(label=label, caption=strip_md(m.group(3))[:300], page=doc.page[i]))
    return out


def activities(doc: Doc, prefixes: list[str]) -> list[Activity]:
    out = []
    for i, line in enumerate(doc.lines):
        h = doc.heading(i)
        if not h or not any(key(h[1]).startswith(key(p)) for p in prefixes):
            continue
        j, body = i + 1, []
        while j < len(doc.lines) and len(" ".join(body)) < 400:
            if doc.heading(j):
                break
            if doc.lines[j].strip() and not PAGE_RE.fullmatch(doc.lines[j].strip()):
                body.append(strip_md(doc.lines[j]))
            j += 1
        out.append(Activity(label=strip_md(h[1]), description=" ".join(body)[:500], page=doc.page[i]))
    return out


def chapter_shell(entry: dict) -> dict:
    return {"chapter_id": entry["chapter_id"], "class": entry["class"], "subject": entry["subject"], "book": entry["book"],
            "part": entry["part"], "chapter_no": entry["chapter_no"], "title": entry["title"]}


def verify(ch: Chapter, doc: Doc) -> list[str]:
    """Every copied piece must really be in the Markdown; all Markdown must be inside some section."""
    warnings = []
    whole = squash(doc.markdown)
    for s in ch.sections:
        if squash(s.text) not in whole:
            warnings.append(f"section {s.section_id} text is not a copy of the source")
    for e in ch.exercises:
        if squash(e.text) not in whole:
            warnings.append(f"exercise {e.label} text is not a copy of the source")
    covered = sum(len(squash(s.text).split()) for s in ch.sections)
    total = len(whole.split())
    if covered < total:
        warnings.append(f"sections cover {covered} of {total} words")
    if not ch.exercises:
        warnings.append("no exercises found")
    if not ch.summary:
        warnings.append("no summary")
    for s in ch.sections:
        if len(strip_md(s.text)) < 80:
            warnings.append(f"very short section {s.section_id} '{s.heading}'")
    return warnings


# ---------------------------------------------------------------------------------------------
# Heuristic mode
# ---------------------------------------------------------------------------------------------

def heuristic(entry: dict, doc: Doc, cfg: dict) -> Chapter:
    cid = entry["chapter_id"]
    boundary_names = cfg.get("summary", []) + cfg.get("keywords", []) + cfg.get("end_exercises", []) + cfg.get("end_matter", []) \
        + [ANSWERS_HEADING.lstrip("# ")]
    answers_at = next((i for i, line in enumerate(doc.lines) if line.strip() == ANSWERS_HEADING), len(doc.lines))
    starts = []
    for i in range(len(doc.lines)):
        h = doc.heading(i)
        if not h:
            continue
        level, text = h
        plain_heading = level <= cfg.get("section_level", 0) and not matches(text, cfg.get("in_chapter_exercises", []) + cfg.get("boxes", []))
        if level == 1 or (level <= 4 and NUMBERED_RE.match(text)) or matches(text, boundary_names) \
                or matches(text, cfg.get("intro_box", [])) or plain_heading:
            starts.append((i, text))
    sections = build_sections(doc, cid, starts)

    # Exercises: end-of-chapter sections, and in-chapter boxes (until the next heading at the same level or above).
    exercises: list[Exercise] = []
    bounds = [(s, doc.lines.index(s.text.split("\n")[0])) for s in sections]
    starts_idx = sorted(i for i, _ in starts)
    for i in range(answers_at):  # the book's answer pages are not exercises
        h = doc.heading(i)
        if not h:
            continue
        level, text = h
        if matches(text, cfg.get("end_exercises", [])):
            end = min(next((j for j in starts_idx if j > i), len(doc.lines)), answers_at)
            exercises += split_items(doc, i + 1, end, strip_md(text), "end")
        elif matches(text, cfg.get("in_chapter_exercises", [])):
            end = i + 1
            while end < answers_at:
                hh = doc.heading(end)
                if hh and (hh[0] <= level or end in starts_idx):
                    break
                end += 1
            label = f"{strip_md(text)} (p. {doc.page[i]})"
            exercises += split_items(doc, i + 1, end, label, "in_chapter")

    summary = ""
    for s in sections:
        if matches(s.heading, cfg.get("summary", [])):
            summary = strip_md("\n".join(s.text.split("\n")[1:]))
            break
    if not summary:  # opening paragraph of the chapter
        paras = [strip_md(p) for p in doc.markdown.split("\n\n") if strip_md(p) and not p.lstrip().startswith(("#", "<!--", "|", ">"))]
        summary = " ".join(paras[:2])[:700]

    numbered = [re.sub(r"(\s+[a-z]{1,2})+$", "", re.sub(r"^\d+(\.\d+)*\.?\s*", "", s.heading))
                for s in sections if NUMBERED_RE.match(s.heading)]
    kts = key_terms(doc, sections, cfg.get("keywords", []), find_glossary(entry))
    topics = list(dict.fromkeys([t for t in numbered if t] + [k.term for k in kts if not k.meaning.startswith("(glossary)")]))[:30]
    a, b = doc.page_range(0, len(doc.lines))
    ch = Chapter(**chapter_shell(entry), summary=summary, sections=sections, key_terms=kts,
                 activities=activities(doc, cfg.get("activity_prefix", ["Activity"])), figures=figures(doc),
                 exercises=exercises, topics=topics, page_start=a, page_end=b, structured_by="heuristic")
    ch.warnings = verify(ch, doc)
    return ch


# ---------------------------------------------------------------------------------------------
# LLM mode
# ---------------------------------------------------------------------------------------------

LLM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "topics", "sections", "key_terms", "activities", "figures", "exercises"],
    "properties": {
        "summary": {"type": "string", "description": "5-8 lines, plain English, what the chapter teaches"},
        "topics": {"type": "array", "items": {"type": "string"}, "description": "short topic tags for search"},
        "sections": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["heading", "start_line"],
            "properties": {"heading": {"type": "string"}, "start_line": {"type": "integer"}}}},
        "key_terms": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["term", "meaning", "line"],
            "properties": {"term": {"type": "string"}, "meaning": {"type": "string"}, "line": {"type": "integer"}}}},
        "activities": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["label", "start_line", "end_line"],
            "properties": {"label": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}}},
        "figures": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["label", "caption", "line"],
            "properties": {"label": {"type": "string"}, "caption": {"type": "string"}, "line": {"type": "integer"}}}},
        "exercises": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["label", "start_line", "end_line", "type_hint", "source"],
            "properties": {
                "label": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer", "description": "inclusive"},
                "type_hint": {"type": "string", "enum": ["mcq", "short", "long", "fill_blank", "true_false", "match",
                                                         "numerical", "diagram", "activity", "other"]},
                "source": {"type": "string", "enum": ["end", "in_chapter"]}}}},
    },
}

LLM_SYSTEM = """You label the structure of one chapter of a new NCERT textbook (CBSE Class 6-7).
You get the chapter as numbered lines ("N: text"). You do NOT copy or rewrite book text: you return
line numbers, and code cuts the text out. Return:
- sections: where each section starts (start_line = the line of its heading). Split at the chapter's
  numbered sections (e.g. "2.1 ...") and at end matter (summary, keywords, end exercises, projects).
  The first section starts at line 0. Boxes inside a section (activities, "Do you know?", tables) stay inside it.
- exercises: EVERY question the book asks students, one entry per question (sub-parts (a), (b) stay
  with their question): the end-of-chapter exercises (source "end") and in-chapter question boxes
  (source "in_chapter"). Use the exact line range. Label like "<box heading> Q<number>".
  Exercise headings used by this book: {exercise_names}.
- activities: each activity box, label as printed ("Activity 5.2: ..."), with its line range.
- figures: each figure caption ("Fig. 5.3") with its caption text and line.
- key_terms: important terms the chapter defines; meaning = the defining sentence COPIED EXACTLY from the line given.
- summary: 5-8 lines in plain, simple English of what the chapter teaches (this one you write).
- topics: 8-25 short topic tags a teacher might search for ("photosynthesis", "Harappan drains").
If the chapter ends with "## Answers to the chapter's questions (from the book)", that part is the book's
answer key: make it one section of its own, and do not list its questions as exercises."""


def llm_mode(entry: dict, doc: Doc, cfg: dict) -> Chapter:
    from ingest import llm

    names = cfg.get("end_exercises", []) + cfg.get("in_chapter_exercises", [])
    system = LLM_SYSTEM.replace("{exercise_names}", ", ".join(f"'{n}'" for n in names) or "(see the chapter)")
    prompt = (f"Chapter {entry['chapter_no']}: {entry['title']} (Class {entry['class']}, {entry['book']})\n\n"
              f"{doc.numbered()}")
    data = llm.complete(system, [llm.text_block(prompt)], max_tokens=32000, effort="high", schema=LLM_SCHEMA)
    n = len(doc.lines)
    clamp = lambda i: max(0, min(int(i), n - 1))  # noqa: E731
    cid = entry["chapter_id"]
    sections = build_sections(doc, cid, [(clamp(s["start_line"]), s["heading"]) for s in data["sections"]])
    exercises = []
    for e in data["exercises"]:
        a, b = clamp(e["start_line"]), clamp(e["end_line"]) + 1
        if b > a:
            exercises.append(Exercise(label=e["label"], text=doc.text(a, b), type_hint=e["type_hint"], page=doc.page[a], source=e["source"]))
    acts = []
    for a_ in data["activities"]:
        a, b = clamp(a_["start_line"]), clamp(a_["end_line"]) + 1
        acts.append(Activity(label=a_["label"], description=strip_md(doc.text(a + 1, b))[:500], page=doc.page[a]))
    figs = [Figure(label=f["label"], caption=f["caption"], page=doc.page[clamp(f["line"])]) for f in data["figures"]]
    kts = [KeyTerm(term=k["term"], meaning=k["meaning"], page=doc.page[clamp(k["line"])]) for k in data["key_terms"]]
    gloss = find_glossary(entry)
    have = {k.term.lower() for k in kts}
    for k in key_terms(doc, sections, [], gloss):
        if k.meaning.startswith("(glossary)") and k.term.lower() not in have:
            kts.append(k)
    a, b = doc.page_range(0, n)
    ch = Chapter(**chapter_shell(entry), summary=data["summary"], sections=sections, key_terms=kts, activities=acts,
                 figures=figs, exercises=exercises, topics=data["topics"], page_start=a, page_end=b,
                 structured_by=f"llm:{llm.MODEL}")  # "llm:sonnet" via Claude Code, or the API model id
    warnings = verify(ch, doc)
    whole = squash(doc.markdown)
    for k in kts:
        if k.meaning and not k.meaning.startswith("(glossary)") and squash(k.meaning) not in whole:
            warnings.append(f"key term '{k.term}' meaning is not copied from the book")
    ch.warnings = warnings
    return ch


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chapter_ids", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--mode", choices=["heuristic", "llm"])
    ap.add_argument("--force", action="store_true", help="redo chapters that already have JSON")
    args = ap.parse_args()
    if not args.chapter_ids and not args.all:
        ap.error("give chapter ids or --all")
    mode = args.mode or "heuristic"
    cfg_all = load_book_config()
    STRUCTURED.mkdir(parents=True, exist_ok=True)
    entries = select(load_manifest(), args.chapter_ids or None)
    for e in entries:
        src = EXTRACTED / f"{e['chapter_id']}.md"
        out = STRUCTURED / f"{e['chapter_id']}.json"
        if not src.exists():
            print(f"SKIP {e['chapter_id']}: run ingest.extract_text first")
            continue
        if out.exists() and not args.force and out.stat().st_mtime > src.stat().st_mtime:
            old = json.loads(out.read_text())
            # Never replace an LLM structure with a heuristic one by accident.
            if mode == "heuristic" or old.get("structured_by", "").startswith("llm"):
                continue
        doc = Doc(src.read_text(encoding="utf-8"))
        cfg = cfg_all.get(e["subject"], {})
        ch = llm_mode(e, doc, cfg) if mode == "llm" else heuristic(e, doc, cfg)
        out.write_text(json.dumps(ch.dump(), indent=1, ensure_ascii=False), encoding="utf-8")
        warn = f"  warnings: {'; '.join(ch.warnings[:4])}" if ch.warnings else ""
        print(f"{e['chapter_id']:<10} {len(ch.sections):>3} sections {len(ch.exercises):>3} exercises "
              f"{len(ch.key_terms):>3} terms {len(ch.figures):>3} figs{warn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
