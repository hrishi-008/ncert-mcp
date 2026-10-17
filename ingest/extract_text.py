"""Stage 1a: chapter PDF -> Markdown with pymupdf (all subjects; Maths is later replaced by stage 1b).

What it keeps: every piece of text on every page, in reading order. Headings (by font size),
bold key terms, side notes in the margin (as "> " quotes), activity boxes, figure captions,
table cells (one table row per line, cells split by " | "), and page markers <!-- page N -->.

What it drops (noise only): running headers in the margin ("Curiosity | Textbook of Science |
Grade 6"), the printed page number, and the "Reprint 2026-27" footer.

Completeness check: after writing, every word in the PDF (minus the noise above) must appear in the
Markdown, the same number of times. Any shortfall is written to data/extracted/<id>.check.json and
printed as an error. Run `python -m ingest.extract_text --all` and expect "0 missing words".

Usage:
    python -m ingest.extract_text 7-sci-05          # one chapter
    python -m ingest.extract_text --all             # every chapter in books.yaml
    python -m ingest.extract_text --all --force     # ignore the cache
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

from ingest.common import DATA, EXTRACTED, clean_text, load_manifest, select, words

BOLD_FLAG = 16
NOISE_PATTERNS = [
    re.compile(r"^\s*reprint\s+20\d\d\s*-\s*\d\d\s*$", re.I),
    re.compile(r"^\s*not to be republished\s*$", re.I),
    re.compile(r"^\s*©\s*ncert", re.I),
]
# Private-use glyphs NCERT uses as bullets / icons.
PUA = re.compile("[\x80-\x9f\ue000-\uf8ff\ufffd]")
ITEM_START = re.compile(r"^(•|[-–]\s|\d{1,2}[.)]\s|\(?[a-z]\)\s|\(?[ivx]{1,4}\)\s|\(\d{1,2}\)\s)")


@dataclass
class Block:
    x0: float
    y0: float
    x1: float
    y1: float
    lines: list[list[dict]]  # spans per line
    size: float  # dominant font size
    bold: bool
    text: str
    kind: str = "body"  # body / heading / margin
    level: int = 0


@dataclass
class PageResult:
    printed_no: int | None
    blocks: list[Block] = field(default_factory=list)
    expected: Counter = field(default_factory=Counter)
    dropped: list[str] = field(default_factory=list)


def span_text(s: dict) -> str:
    return clean_text(s["text"])


def block_from_dict(b: dict) -> Block | None:
    lines = []
    for line in b.get("lines", []):
        spans = [s for s in line["spans"] if s["text"].strip()]
        if spans:
            lines.append({"spans": spans, "dir": line.get("dir", (1, 0))})
    if not lines:
        return None
    chars = Counter()
    bold_chars = 0
    total = 0
    for line in lines:
        for s in line["spans"]:
            n = len(s["text"].strip())
            chars[round(s["size"], 1)] += n
            total += n
            if s["flags"] & BOLD_FLAG or "bold" in s["font"].lower() or "black" in s["font"].lower():
                bold_chars += n
    size = chars.most_common(1)[0][0]
    text = " ".join("".join(span_text(s) for s in line["spans"]).strip() for line in lines)
    x0, y0, x1, y1 = b["bbox"]
    blk = Block(x0, y0, x1, y1, [line["spans"] for line in lines], size, bold_chars > total * 0.6, text.strip())
    blk.rotated = any(abs(line["dir"][0]) < 0.9 for line in lines)  # type: ignore[attr-defined]
    return blk


def is_noise(blk: Block, page_w: float, page_h: float, book_words: set[str]) -> str | None:
    """Return why this block is noise, or None if it is real content."""
    t = blk.text.strip()
    if any(p.search(t) for p in NOISE_PATTERNS):
        return "footer"
    if getattr(blk, "rotated", False):
        # Running header printed sideways in the margin, e.g. "Curiosity | Textbook of Science | Grade 6"
        # or the chapter name. Only drop it if it is short.
        if len(t) < 90:
            return "running header"
    in_margin = blk.x1 < page_w * 0.13 or blk.x0 > page_w * 0.87 or blk.y1 < page_h * 0.07 or blk.y0 > page_h * 0.93
    if re.fullmatch(r"\d{1,3}", t) and in_margin:
        return "page number"
    low = t.lower()
    if in_margin and len(t) < 90 and ("|" in t or "grade" in low) and book_words & set(re.findall(r"[a-z]+", low)):
        return "running header"
    return None


def detect_page_number(blocks: list[Block], page_w: float, page_h: float) -> int | None:
    for blk in blocks:
        t = blk.text.strip()
        in_margin = blk.x1 < page_w * 0.13 or blk.x0 > page_w * 0.87 or blk.y1 < page_h * 0.07 or blk.y0 > page_h * 0.9
        if re.fullmatch(r"\d{1,3}", t) and in_margin:
            return int(t)
    return None


def render_block(blk: Block) -> str:
    """Block -> Markdown text, keeping bold spans as **term**."""
    if blk.kind == "table":
        return blk.text
    out_lines = []
    for spans in blk.lines:
        parts = []
        for s in spans:
            txt = span_text(s)
            is_bold = s["flags"] & BOLD_FLAG or "bold" in s["font"].lower()
            if is_bold and not blk.bold and blk.kind == "body" and txt.strip() and re.search(r"[A-Za-z]", txt):
                lead = txt[: len(txt) - len(txt.lstrip())]
                trail = txt[len(txt.rstrip()):]
                txt = f"{lead}**{txt.strip()}**{trail}"
            parts.append(txt)
        line = "".join(parts).strip()
        line = PUA.sub("•", line)
        out_lines.append(line)
    # Join lines into one paragraph; keep bullets / numbered items on their own lines.
    text = ""
    for line in out_lines:
        if not line:
            continue
        starts_item = bool(ITEM_START.match(line))
        if not text:
            text = line
        elif starts_item:
            text += "\n" + line
        elif text.endswith("-") and not text.endswith(" -"):
            text += line  # word broken across lines: keep the hyphen, join without a space
        else:
            text += " " + line
    text = re.sub(r"(^|\n)\s*(?:•\s*)+", r"\1- ", text)
    text = re.sub(r"\s*•\s*", " ", text)
    text = re.sub(r"\*\*\s*\*\*", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def analyse_page(page: pymupdf.Page, book_words: set[str]) -> PageResult:
    w, h = page.rect.width, page.rect.height
    raw = page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP)["blocks"]
    blocks = [b for b in (block_from_dict(r) for r in raw if r["type"] == 0) if b]
    res = PageResult(printed_no=detect_page_number(blocks, w, h))
    for blk in blocks:
        why = is_noise(blk, w, h, book_words)
        if why:
            res.dropped.append(f"[{why}] {blk.text}")
        else:
            res.blocks.append(blk)
    res.expected = Counter(w for b in res.blocks for w in words(b.text))
    res.blocks = pull_tables(page, res.blocks)
    return res


def _inside(blk: Block, x0: float, y0: float, x1: float, y1: float) -> bool:
    cx, cy = (blk.x0 + blk.x1) / 2, (blk.y0 + blk.y1) / 2
    return x0 - 2 <= cx <= x1 + 2 and y0 - 2 <= cy <= y1 + 2


def pull_tables(page: pymupdf.Page, blocks: list[Block]) -> list[Block]:
    """Replace the text blocks of each ruled table with one Markdown table block.

    pymupdf finds the ruled grid but often misses a header row printed above the top rule,
    so header cells are rebuilt from the text blocks just above the grid, by column.
    """
    try:
        tables = page.find_tables().tables
    except Exception:
        return blocks
    for t in tables:
        if t.col_count < 2 or t.row_count < 2:
            continue  # a ruled box around a paragraph, not a table
        x0, y0, x1, y1 = t.bbox
        rows = [[clean_text(c or "").replace("\n", " ").strip() for c in r] for r in t.extract()]
        if any(len(c) > 200 for r in rows for c in r):
            continue  # a cell holding whole paragraphs means the grid was mis-detected; keep plain text
        if sum(1 for r in rows for c in r if c) < 3:
            continue
        cols = [c[0] for c in t.rows[0].cells if c] + [x1]
        # Header: blocks just above the grid, inside its width, that are not the caption.
        above = [
            b for b in blocks
            if b.y1 <= y0 + 2 and b.y0 >= y0 - 80 and b.x0 >= x0 - 5 and b.x1 <= x1 + 5
            and not re.match(r"(?i)^(table|fig)", b.text)
        ]
        header = [""] * len(rows[0])
        used = []
        if above and len({round(b.x0) for b in above}) >= 2:
            for b in sorted(above, key=lambda b: (b.y0, b.x0)):
                cx = (b.x0 + b.x1) / 2
                for i in range(min(len(cols) - 1, len(header))):
                    if cols[i] - 2 <= cx <= cols[i + 1] + 2:
                        header[i] = (header[i] + " " + b.text).strip()
                        used.append(b)
                        break
        inside = [b for b in blocks if _inside(b, x0, y0, x1, y1)]
        if not inside:
            continue
        def row_md(r: list[str]) -> str:
            return "| " + " | ".join(c.replace("|", "/") for c in r) + " |"
        if any(header):
            md_rows = [row_md(header), row_md(["---"] * len(header))] + [row_md(r) for r in rows]
        else:
            md_rows = [row_md(rows[0]), row_md(["---"] * len(rows[0]))] + [row_md(r) for r in rows[1:]]
        top = min([y0] + [b.y0 for b in used])
        # Keep any text inside the grid that the table cells missed, so nothing is lost.
        cell_words = Counter(words(" ".join(" ".join(r) for r in rows)))
        leftovers = []
        for b in inside:
            need = Counter(words(b.text))
            if need - cell_words:
                leftovers.append(b.text)
            else:
                cell_words -= need
        text = "\n".join(md_rows)
        if leftovers:
            text += "\n\n" + "\n".join(leftovers)
        tb = Block(x0, top, x1, y1, [], 0.0, False, text, kind="table")
        consumed = {id(b) for b in inside + used}
        blocks = [b for b in blocks if id(b) not in consumed] + [tb]
    return blocks


def classify(pages: list[PageResult]) -> float:
    """Mark headings and margin notes. Returns the body font size."""
    size_chars: Counter = Counter()
    for p in pages:
        for blk in p.blocks:
            if blk.kind != "table":
                size_chars[blk.size] += len(blk.text)
    body = size_chars.most_common(1)[0][0] if size_chars else 12.0
    heading_sizes = sorted(
        {blk.size for p in pages for blk in p.blocks
         if blk.kind != "table" and blk.size >= body + 1.5 and len(blk.text) < 160 and _latin(blk.text)},
        reverse=True,
    )
    level_of = {s: min(i + 2, 4) for i, s in enumerate(heading_sizes)}  # "#" is kept for the chapter title
    for p in pages:
        mx0, mx1 = main_column(p, body)
        for blk in p.blocks:
            if blk.kind == "table":
                continue
            cx = (blk.x0 + blk.x1) / 2
            if mx1 > mx0 and (cx < mx0 - 5 or cx > mx1 + 5) and blk.x1 - blk.x0 < 160:
                blk.kind = "margin"  # side note beside the main column
            elif blk.size in level_of and len(blk.text) < 160 and _latin(blk.text):
                blk.kind, blk.level = "heading", level_of[blk.size]
            elif blk.bold and len(blk.text) < 100 and blk.size >= body - 0.6 and not blk.text.endswith((".", ",", "?")) and _latin(blk.text):
                blk.kind, blk.level = "heading", min(len(heading_sizes) + 2, 5)
    return body


def _latin(text: str) -> bool:
    letters = re.findall(r"[^\W\d_]", text)
    return bool(letters) and sum(c.isascii() for c in letters) > len(letters) * 0.8


def main_column(p: PageResult, body: float) -> tuple[float, float]:
    """x-range of the main text column on this page (pages alternate left/right margins)."""
    xs0, xs1 = Counter(), Counter()
    for blk in p.blocks:
        if blk.kind != "table" and abs(blk.size - body) < 0.6 and len(blk.text) > 60:
            xs0[round(blk.x0 / 5) * 5] += len(blk.text)
            xs1[round(blk.x1 / 5) * 5] += len(blk.text)
    if not xs0:
        return 0.0, 0.0
    return xs0.most_common(1)[0][0], xs1.most_common(1)[0][0]


def order_blocks(blocks: list[Block]) -> list[list[Block]]:
    """Sort into reading order. Blocks side by side at the same height form one row (table cells)."""
    blocks = sorted(blocks, key=lambda b: (b.y0, b.x0))
    rows: list[list[Block]] = []
    for blk in blocks:
        if blk.kind not in ("margin", "table") and rows:
            row = rows[-1]
            last = row[-1]
            same_band = abs(blk.y0 - row[0].y0) < 8 and row[0].kind not in ("margin", "table")
            beside = all(blk.x0 >= r.x1 - 2 or blk.x1 <= r.x0 + 2 for r in row)
            short = len(blk.text) < 150 and all(len(r.text) < 150 for r in row)
            if same_band and beside and short and last.kind != "heading" and blk.kind != "heading":
                row.append(blk)
                continue
        rows.append([blk])
    for row in rows:
        row.sort(key=lambda b: b.x0)
    return rows


def extract_chapter(entry: dict) -> tuple[str, dict]:
    pdf = DATA / entry["file"]
    doc = pymupdf.open(pdf)
    book_words = set(re.findall(r"[a-z]+", entry["book"].lower())) | {"textbook", "grade", "science", "mathematics"}
    pages = [analyse_page(p, book_words) for p in doc]

    # Fill in printed page numbers that weren't found, from neighbours.
    known = [(i, p.printed_no) for i, p in enumerate(pages) if p.printed_no]
    offset = Counter(n - i for i, n in known).most_common(1)[0][0] if known else 1
    for i, p in enumerate(pages):
        if p.printed_no is None or p.printed_no - i != offset:
            p.printed_no = i + offset

    classify(pages)
    md = [f"# Chapter {entry['chapter_no']}: {entry['title']}", ""]
    for p in pages:
        md.append(f"<!-- page {p.printed_no} -->")
        md.append("")
        for row in order_blocks(p.blocks):
            if len(row) > 1 and all(b.kind != "table" for b in row):
                md.append(" | ".join(render_block(b).replace("\n", " ") for b in row))
            else:
                blk = row[0]
                text = render_block(blk)
                if blk.kind == "table":
                    md.append(text)
                elif blk.kind == "heading":
                    md.append("#" * blk.level + " " + text.replace("\n", " ").replace("**", ""))
                elif blk.kind == "margin":
                    md.append("\n".join("> " + t for t in text.split("\n")))
                else:
                    md.append(text)
            md.append("")
    markdown = re.sub(r"\n{3,}", "\n\n", "\n".join(md)).strip() + "\n"

    # Completeness check: every kept word must be in the Markdown, as many times as in the PDF.
    expected = Counter()
    for p in pages:
        expected.update(p.expected)
    got = Counter(words(markdown))
    missing = expected - got
    report = {
        "chapter_id": entry["chapter_id"],
        "pdf_pages": doc.page_count,
        "printed_pages": [pages[0].printed_no, pages[-1].printed_no],
        "words_expected": sum(expected.values()),
        "words_in_markdown": sum(got.values()),
        "missing_words": sum(missing.values()),
        "missing_sample": sorted(missing.items(), key=lambda kv: -kv[1])[:30],
        "dropped_as_noise": [d for p in pages for d in p.dropped],
        "image_only_pages": [p.printed_no for p in pages if not p.blocks],
    }
    return markdown, report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chapter_ids", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--subject", action="append", help="limit to sci / sst / math (repeatable)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not args.chapter_ids and not args.all:
        ap.error("give chapter ids or --all")
    entries = select(load_manifest(), args.chapter_ids or None, args.subject)
    EXTRACTED.mkdir(parents=True, exist_ok=True)
    bad = 0
    for e in entries:
        out = EXTRACTED / f"{e['chapter_id']}.md"
        check = EXTRACTED / f"{e['chapter_id']}.check.json"
        src = DATA / e["file"]
        if out.exists() and check.exists() and out.stat().st_mtime > src.stat().st_mtime and not args.force:
            report = json.loads(check.read_text())
        else:
            markdown, report = extract_chapter(e)
            out.write_text(markdown, encoding="utf-8")
            check.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        status = "OK " if report["missing_words"] == 0 else "ERR"
        bad += report["missing_words"] > 0
        extra = f", image-only pages {report['image_only_pages']}" if report["image_only_pages"] else ""
        print(
            f"{status} {e['chapter_id']:<10} pages {report['printed_pages'][0]}-{report['printed_pages'][1]}  "
            f"{report['words_expected']:>6} words, {report['missing_words']} missing, "
            f"{len(report['dropped_as_noise'])} noise lines dropped{extra}  {e['title']}"
        )
    print(f"\n{len(entries)} chapters, {bad} with missing words.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
