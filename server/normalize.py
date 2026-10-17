"""Turn loose teacher input ("VII", "std 6", "Curiosity", "ch5") into canonical values.

Bad input raises NormalizeError with a helpful message (never a bare "not found").
"""

from __future__ import annotations

import re

CLASSES = (6, 7)
SUBJECTS = ("sci", "math", "sst")
SUBJECT_NAMES = {"sci": "Science (Curiosity)", "math": "Maths (Ganita Prakash)", "sst": "SST (Exploring Society: India and Beyond)"}


class NormalizeError(ValueError):
    pass


_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10, "xi": 11, "xii": 12}
_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7,
    "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13,
    "fourteenth": 14, "fifteenth": 15, "last": -1,
}

# Longest aliases first so "social science" wins over "science".
_SUBJECT_ALIASES = sorted(
    {
        "sci": "sci", "science": "sci", "curiosity": "sci", "evs": "sci",
        "physics": "sci", "chemistry": "sci", "biology": "sci", "bio": "sci",
        "maths": "math", "math": "math", "mathematics": "math", "ganita": "math",
        "ganita prakash": "math", "ganit": "math", "gp": "math",
        "sst": "sst", "ss": "sst", "social": "sst", "social science": "sst",
        "social sciences": "sst", "social studies": "sst", "exploring society": "sst",
        "exploring society india and beyond": "sst", "history": "sst", "geography": "sst",
        "civics": "sst", "economics": "sst", "polity": "sst", "political science": "sst",
    }.items(),
    key=lambda kv: -len(kv[0]),
)


def _to_int(token: str) -> int | None:
    token = token.lower().strip(". ")
    if token.isdigit():
        return int(token)
    m = re.fullmatch(r"(\d+)(st|nd|rd|th)", token)
    if m:
        return int(m.group(1))
    if token in _ROMAN:
        return _ROMAN[token]
    return _WORD_NUMS.get(token)


def normalize_class(value: object) -> int:
    """6, "VI", "class 6", "std 6", "6th", "grade VII" -> 6 or 7."""
    if isinstance(value, int) and not isinstance(value, bool):
        n = value
    else:
        text = str(value).strip().lower()
        text = re.sub(r"^(class|std|standard|grade|cls|kaksha)\.?\s*[-:]?\s*", "", text)
        text = re.sub(r"\s*(class|std|standard|grade)$", "", text)
        n = _to_int(text)
        if n is None:
            raise NormalizeError(f"I couldn't read the class from {value!r}. Use 6 or 7 (e.g. '7', 'VII', 'class 7').")
    if n not in CLASSES:
        raise NormalizeError(f"Class {n} isn't in the books I have. I only have Class 6 and Class 7.")
    return n


def normalize_subject(value: object) -> str:
    """'sci', 'Curiosity', 'Maths', 'Ganita Prakash', 'social science', 'history' -> sci/math/sst."""
    text = re.sub(r"[^a-z ]+", " ", str(value).lower())
    text = re.sub(r"\s+", " ", text).strip()
    for alias, code in _SUBJECT_ALIASES:
        if text == alias:
            return code
    for alias, code in _SUBJECT_ALIASES:
        if re.search(rf"\b{re.escape(alias)}\b", text):
            return code
    raise NormalizeError(
        f"I couldn't tell the subject from {value!r}. Use one of: science / sci / Curiosity, "
        "maths / Ganita Prakash, sst / social science / history / geography / civics."
    )


def normalize_chapter_id(value: object) -> str:
    """'7-sci-5', '7 sci 05', '7-SCI-05' -> '7-sci-05'. Raises NormalizeError if it can't."""
    text = str(value).strip().lower()
    m = re.fullmatch(r"(\d+|[ivx]+)\s*[-_ /]\s*([a-z ]+?)\s*[-_ /]\s*(\d{1,2})", text)
    if not m:
        raise NormalizeError(
            f"{value!r} doesn't look like a chapter id. Chapter ids look like '7-sci-05' or '6-math-03'. "
            "Use find_chapter to get the right id."
        )
    return f"{normalize_class(m.group(1))}-{normalize_subject(m.group(2))}-{int(m.group(3)):02d}"


def make_chapter_id(cls: int, subject: str, chapter_no: int) -> str:
    return f"{cls}-{subject}-{chapter_no:02d}"


def parse_query(query: str) -> dict:
    """Pull class / subject / chapter number out of free text.

    "VII sci ch5" -> {class: 7, subject: 'sci', chapter_no: 5, rest: ''}
    "std 6 maths 3rd chapter" -> {class: 6, subject: 'math', chapter_no: 3, rest: ''}
    "the plants chapter" -> {rest: 'plants'}
    """
    text = " " + str(query).lower().replace("’", "'") + " "
    out: dict = {"class": None, "subject": None, "chapter_no": None}

    # Class: "class 7", "std vii", "grade 6", "7th class", "class-VI", or a bare roman numeral.
    m = re.search(r"\b(?:class|std|standard|grade|cls)\.?\s*[-:]?\s*(\d{1,2}|[ivx]{1,4})(?:st|nd|rd|th)?\b", text)
    if not m:
        m = re.search(r"\b(\d{1,2}|[ivx]{1,4})(?:st|nd|rd|th)?\s+(?:class|std|standard|grade)\b", text)
    if not m:
        m = re.search(r"(?<![\w-])(vi|vii)(?![\w-])", text)
    if m:
        n = _to_int(m.group(1))
        if n is not None:
            out["class"] = n
            text = text[: m.start()] + " " + text[m.end():]

    # Chapter: "ch 5", "ch5", "chapter 5", "chapter five", "lesson 3", "5th chapter", "3rd one", "chapter no. 4".
    num = r"(\d{1,2}(?:st|nd|rd|th)?|" + "|".join(sorted(_WORD_NUMS, key=len, reverse=True)) + ")"
    pats = [
        rf"\b(?:ch|chap|chapter|lesson|unit|ch\.)\s*(?:no\.?|number|#)?\s*[-:]?\s*{num}\b",
        rf"\b{num}\s+(?:ch|chap|chapter|lesson|unit|one)\b",
    ]
    for pat in pats:
        m = re.search(pat, text)
        if m:
            n = _to_int(m.group(1))
            if n is not None:
                out["chapter_no"] = n
                text = text[: m.start()] + " " + text[m.end():]
                break

    # Subject words (keep topic words like "history" meaningful: only strip clear subject names).
    for alias, code in _SUBJECT_ALIASES:
        if alias in ("history", "geography", "civics", "economics", "physics", "chemistry", "biology", "bio", "gp", "ss"):
            continue
        m = re.search(rf"\b{re.escape(alias)}\b", text)
        if m:
            out["subject"] = code
            text = text[: m.start()] + " " + text[m.end():]
            break

    # A bare leading class number like "7 sci 5" or "6 maths".
    if out["class"] is None:
        m = re.match(r"\s*([67])\b", text)
        if m and (out["subject"] or out["chapter_no"]):
            out["class"] = int(m.group(1))
            text = text[: m.start()] + " " + text[m.end():]
    # "7 sci 5": a bare trailing number after class+subject is the chapter.
    if out["chapter_no"] is None and out["subject"]:
        m = re.fullmatch(r"\s*(\d{1,2})\s*", text)
        if m:
            out["chapter_no"] = int(m.group(1))
            text = ""

    out["rest"] = re.sub(r"\s+", " ", text).strip()
    return out
