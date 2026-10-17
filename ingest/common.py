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


def clean_text(text: str) -> str:
    """Fix PDF quirks: ligatures (and the stray space NCERT PDFs put after them), odd spaces."""
    for lig, rep in LIGATURES.items():
        # "diﬀ erent" -> "different", "ﬁ eld" -> "field"
        text = re.sub(re.escape(lig) + r" (?=[a-z])", rep, text)
        text = text.replace(lig, rep)
    text = text.replace("­", "")  # soft hyphen
    text = re.sub(r"[ -   　 ]", " ", text)  # thin/odd spaces
    text = text.replace("​", "").replace("﻿", "")
    return text


def words(text: str) -> list[str]:
    """Normalised word list, used to prove nothing was lost or invented."""
    text = unicodedata.normalize("NFKC", clean_text(text)).lower()
    return re.findall(r"[a-z0-9]+", text)


def squash(text: str) -> str:
    """Whitespace/markup-insensitive form for substring checks."""
    return " ".join(words(text))
