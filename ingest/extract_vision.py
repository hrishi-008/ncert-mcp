"""Stage 1b: Maths chapter PDF -> page images -> Claude vision -> Markdown with LaTeX.

Plain text extraction mangles equations, fractions and tables in Ganita Prakash, so each page is
rendered at ~200 DPI and transcribed by Claude. Results are cached per page (by image hash + prompt
version) in data/extracted/.cache/vision/, so re-runs cost nothing.

Completeness check: the vision Markdown is compared with the pymupdf words for the same page
(stage 1a output must exist). Pages where many plain-text words are missing are flagged for a
look in data/extracted/<id>.vision-check.json.

Usage (needs ANTHROPIC_API_KEY):
    python -m ingest.extract_vision 6-math-01        # one chapter
    python -m ingest.extract_vision --all            # every maths chapter
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pymupdf

from ingest import llm
from ingest.common import DATA, EXTRACTED, load_manifest, select, words
from ingest.extract_text import extract_chapter

PROMPT_VERSION = "v1"
DPI = 200
CACHE = EXTRACTED / ".cache" / "vision"

SYSTEM = """You transcribe pages of the NCERT Class 6-7 mathematics textbook "Ganita Prakash" into Markdown.
Transcribe EVERYTHING printed on the page, in reading order, word for word. Never summarise, shorten,
correct or add content. Rules:
- Use LaTeX for all maths: $...$ inline, $$...$$ for display. Fractions as \\frac{a}{b}.
- Headings as Markdown headings (## for numbered sections like "2.1 ...", ### for smaller ones).
- Keep boxes such as "Figure it Out", "Math Talk", "Try This", "Think", examples and puzzles as headed
  blocks (### Figure it Out) with their numbered questions as a numbered list, numbering exactly as printed.
- Tables as Markdown tables.
- Side notes in the margin as "> " quotes, placed next to the text they sit beside.
- Describe each figure or diagram in one line as [Figure: ...], keeping its label (e.g. "Fig. 2.3") and any
  text, numbers or letters printed inside it.
- Skip only the running header/footer (book name, grade, page number, "Reprint 20xx-xx").
Output only the Markdown for this page."""


def render_pages(pdf_path) -> list[bytes]:
    doc = pymupdf.open(pdf_path)
    zoom = DPI / 72
    return [p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png") for p in doc]


def transcribe(c, png: bytes, page_label: str) -> str:
    key = hashlib.sha256(png + PROMPT_VERSION.encode() + llm.MODEL.encode()).hexdigest()[:32]
    path = CACHE / f"{key}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    resp = llm.create(
        c,
        model=llm.MODEL,
        max_tokens=16000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": "medium"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.standard_b64encode(png).decode()}},
                {"type": "text", "text": f"Transcribe this page (printed page {page_label})."},
            ],
        }],
    )
    md = llm.text_of(resp).strip()
    md = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", md)
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    return md


def run_chapter(c, entry: dict, workers: int) -> dict:
    # Stage 1a gives us printed page numbers and a plain-text word list per page to check against.
    plain_md, plain_report = extract_chapter(entry)
    first_page = plain_report["printed_pages"][0]
    pngs = render_pages(DATA / entry["file"])
    labels = [str(first_page + i) for i in range(len(pngs))]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pages = list(pool.map(lambda a: transcribe(c, *a), zip(pngs, labels)))

    plain_pages = re.split(r"<!-- page \d+ -->", plain_md)[1:]
    out = [f"# Chapter {entry['chapter_no']}: {entry['title']}", ""]
    checks = []
    for label, md, plain in zip(labels, pages, plain_pages + [""] * len(pages)):
        out += [f"<!-- page {label} -->", "", md, ""]
        # Words (not maths) the plain text has but the transcription lacks.
        want = Counter(w for w in words(plain) if w.isalpha() and len(w) > 2)
        got = Counter(w for w in words(md) if w.isalpha())
        missing = want - got
        ratio = sum(missing.values()) / max(1, sum(want.values()))
        checks.append({"page": int(label), "plain_words": sum(want.values()), "missing": sum(missing.values()),
                       "missing_ratio": round(ratio, 3), "sample": [w for w, _ in missing.most_common(12)]})
    (EXTRACTED / f"{entry['chapter_id']}.md").write_text("\n".join(out).strip() + "\n", encoding="utf-8")
    report = {"chapter_id": entry["chapter_id"], "model": llm.MODEL, "pages": checks,
              "flagged_pages": [ch["page"] for ch in checks if ch["missing_ratio"] > 0.05 and ch["missing"] >= 5]}
    (EXTRACTED / f"{entry['chapter_id']}.vision-check.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chapter_ids", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    if not args.chapter_ids and not args.all:
        ap.error("give chapter ids or --all")
    entries = select(load_manifest(), args.chapter_ids or None, ["math"])
    c = llm.client()
    flagged = 0
    for e in entries:
        rep = run_chapter(c, e, args.workers)
        flagged += bool(rep["flagged_pages"])
        note = f"check pages {rep['flagged_pages']}" if rep["flagged_pages"] else "all pages look complete"
        print(f"{e['chapter_id']:<10} {len(rep['pages'])} pages, {note}  {e['title']}")
    print(f"\n{len(entries)} chapters, {flagged} with pages to check (see data/extracted/*.vision-check.json).")
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
