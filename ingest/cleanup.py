"""Deterministic clean-up of structured chapters, applied by build_db before writing books.db.

It only removes print artefacts and moves lines between sections; it never rewrites a word of book text.
  1. Margin-icon lines: the Curiosity "question wheel" (ENQUIRY, W HAT, ...), "?" badges and
     "[Figure: ... icon]" notes that the extractor kept as blockquotes. They clutter exercise text.
  2. Descender debris after numbered headings: "10.3 Transport in Plants p" -> "10.3 Transport in Plants".
  3. A heading with no body of its own (e.g. "10.3" directly followed by "10.3.1") was merged into the
     end of the previous section. Its line is moved to the start of the section it introduces.
"""

from __future__ import annotations

import re

from ingest.schema import Chapter

# Exact blockquote lines that are decorative icons, as the extractor writes them. Map labels and compass
# letters (N, S, "E W", INDIA, ...) look similar, so this is a list of exact strings, not a pattern.
ICON_LINES = {
    "?", "[?]", "(?)", "IT ?", "CHAPTER",
    "CURIOSITY", "ENQUIRY", "EXPLORATION", "DISCOVERY", "INNOVATION",
    "WHY", "WHAT IF", "WHERE", "WHOSE", "ELSE", "HAPPEN", "NOT", "CAN", "W", "H",
    "W HAT", "H O W", "H E N", "I C H", "L O N G", "WHAT WILL",
    "WHAT COULD", "WHAT COULD CAN", "WHAT SHOULD", "WHAT SHOULD WHY",
}
ICON_NOTE_RE = re.compile(r"\[(?:[^\]]*\b(?:icon|Icon)\b[^\]]*)\]")
QUOTE_RE = re.compile(r"^>\s?(.*)$")
NUMBERED_HEAD_RE = re.compile(r"^(#{1,6}\s+)?(\d{1,2}(?:\.\d{1,2})+\.?\s+.*?\S)((?:\s+[gpy]+)+)\s*$")
HEAD_LINE_RE = re.compile(r"^#{1,6}\s+\d{1,2}(?:\.\d{1,2})+\.?\s+\S")  # numbered headings only
PAGE_LINE_RE = re.compile(r"^<!-- (?:answers )?page \d+ -->$")


def is_icon_line(line: str) -> bool:
    m = QUOTE_RE.match(line.strip())
    if not m:
        return False
    body = m.group(1).strip()
    return body in ICON_LINES or bool(ICON_NOTE_RE.fullmatch(body))


def fix_heading(text: str) -> str:
    """Drop stray descender letters (p, g, y) the PDF leaves after a numbered heading."""
    m = NUMBERED_HEAD_RE.match(text)
    return f"{m.group(1) or ''}{m.group(2)}" if m else text


def clean_text(text: str) -> str:
    lines = [fix_heading(line) for line in text.split("\n") if not is_icon_line(line)]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _split_trailing_headings(text: str) -> tuple[str, str]:
    """('body ... ### 10.3 X\\n', '') -> ('body ...', '### 10.3 X'): numbered headings with nothing after them."""
    lines = text.split("\n")
    end = len(lines)
    cut = end
    i = end - 1
    while i >= 0:
        s = lines[i].strip()
        if not s or PAGE_LINE_RE.match(s):
            i -= 1
            continue
        if HEAD_LINE_RE.match(s):
            cut = i
            i -= 1
            continue
        break
    if cut == end or cut == 0:
        return text, ""
    return "\n".join(lines[:cut]).rstrip(), "\n".join(lines[cut:]).strip()


def clean_chapter(ch: Chapter) -> Chapter:
    for s in ch.sections:
        s.heading = fix_heading(s.heading)
        s.text = clean_text(s.text)
    for prev, nxt in zip(ch.sections, ch.sections[1:]):
        body, moved = _split_trailing_headings(prev.text)
        if moved:
            prev.text, nxt.text = body, f"{moved}\n\n{nxt.text}"
    for e in ch.exercises:
        e.text = clean_text(e.text)
    for a in ch.activities:
        a.description = clean_text(a.description)
    return ch
