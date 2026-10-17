"""Paths and helpers shared by the ingestion stages."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MANIFEST = DATA / "books.yaml"
EXTRACTED = DATA / "extracted"
STRUCTURED = DATA / "structured"
BOOK_CONFIG = DATA / "book_config.yaml"
OLD_MAP = DATA / "old_to_new_map.json"
BOOKS_DB = DATA / "books.db"


def chapter_id(entry: dict) -> str:
    return f"{entry['class']}-{entry['subject']}-{entry['chapter_no']:02d}"


def load_manifest() -> list[dict]:
    entries = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or []
    for e in entries:
        e["chapter_id"] = chapter_id(e)
    return entries


def select(entries: list[dict], ids: list[str] | None, subjects: list[str] | None = None) -> list[dict]:
    out = [e for e in entries if (not ids or e["chapter_id"] in ids) and (not subjects or e["subject"] in subjects)]
    if ids:
        missing = set(ids) - {e["chapter_id"] for e in out}
        if missing:
            raise SystemExit(f"Not in books.yaml: {', '.join(sorted(missing))}")
    return out


def load_book_config() -> dict:
    return yaml.safe_load(BOOK_CONFIG.read_text(encoding="utf-8")) or {}


LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}


LIG_GAP = re.compile(r"\b([A-Za-z]*(?:ffi|ffl|fi|fl|ff)) ([a-z]+)")
# Real words ending in these letters, which must not be joined to the next word ("off and", "staff room").
_LIG_WORDS = {"off", "staff", "cliff", "stuff", "cuff", "puff", "sniff", "stiff", "buff", "chaff", "tariff",
              "bluff", "gruff", "scoff", "scuff", "huff", "fluff", "whiff", "wifi", "hifi", "scifi", "kickoff", "takeoff", "layoff", "playoff", "standoff",
              "sheriff", "plaintiff", "mastiff", "pontiff", "rebuff", "dandruff", "handcuff", "snuff", "bailiff"}


def _join_ligature(m: re.Match) -> str:
    left, right = m.group(1), m.group(2)
    if left.lower() in _LIG_WORDS:
        return m.group(0)
    return left + right


def clean_text(text: str) -> str:
    """Fix PDF quirks: ligatures (and the stray space NCERT PDFs put after them), odd spaces."""
    for lig, rep in LIGATURES.items():
        # "diﬀ erent" -> "different", "ﬁ eld" -> "field"
        text = re.sub(re.escape(lig) + r" (?=[a-z])", rep, text)
        text = text.replace(lig, rep)
    text = text.replace("\u00ad", "")  # soft hyphen
    text = re.sub(r"[\u2000-\u200a\u202f\u205f\u3000\u00a0]", " ", text)  # thin/odd spaces
    text = text.replace("\u200b", "").replace("\ufeff", "")  # zero-width space, BOM
    # The PDFs also print ligatures as plain letters followed by a gap: "fl ower", "refl ect", "diffi cult".
    text = LIG_GAP.sub(_join_ligature, text)
    return text


def words(text: str) -> list[str]:
    """Normalised word list, used to prove nothing was lost or invented."""
    text = unicodedata.normalize("NFKC", clean_text(text)).lower()
    return re.findall(r"[a-z0-9]+", text)


def squash(text: str) -> str:
    """Whitespace/markup-insensitive form for substring checks."""
    return " ".join(words(text))


ANSWERS_HEADING = "## Answers to the chapter's questions (from the book)"
_ANSWER_PAGE = re.compile(r"(?im)^\s*page\s*no\.?\s*\d|\bAns\s*[:.]")


def chapter_length(entry: dict) -> int | None:
    """Book pages in this chapter, from the contents page: where the next chapter of the same part starts."""
    nxt = [e for e in load_manifest() if (e["class"], e["subject"], e["part"]) == (entry["class"], entry["subject"], entry["part"])
           and e.get("chapter_in_part") == entry.get("chapter_in_part", 0) + 1]
    if nxt and nxt[0].get("book_page_start") and entry.get("book_page_start"):
        return nxt[0]["book_page_start"] - entry["book_page_start"]
    return None


def answers_start(page_texts: list[str], expected_len: int | None = None) -> int | None:
    """Index of the first answer page, if the chapter PDF ends with the book's answers.

    Ganita Prakash chapter PDFs end with worked answers to the in-text questions, numbered 1, 2, 3 ...
    on their own. The contents page tells where the chapter's own pages end; any pages after that are
    answers. For the last chapter of a book, the answers are found by their "Page No." / "Ans:" lines.
    """
    looks = [bool(_ANSWER_PAGE.search(t)) for t in page_texts]
    if expected_len is not None:
        if len(page_texts) > expected_len and any(looks[expected_len:]):
            return expected_len
        return None
    for i, ok in enumerate(looks):
        if ok and i >= len(page_texts) // 2 and sum(looks[i:]) >= max(1, (len(page_texts) - i) // 2):
            return i
    return None
