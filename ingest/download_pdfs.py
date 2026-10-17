"""Download the newest NCERT books (Class 6 & 7: Science, Maths, SST) from ncert.nic.in.

Book codes are NOT hardcoded. They are read from the book menu on
https://ncert.nic.in/textbook.php, and each code is checked against the book title.

For every book we fetch the prelims (`<code>ps.pdf`, has the contents page) and
every chapter (`<code><NN>.pdf`). Files are saved under data/pdfs/{class}/{subject}/
with their original NCERT names, then checked, and data/books.yaml is written.

Usage:
    python -m ingest.download_pdfs            # download what's missing, then write books.yaml
    python -m ingest.download_pdfs --check    # only re-check files on disk + rewrite books.yaml
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf
import yaml

ROOT = Path(__file__).resolve().parent.parent
PDF_DIR = ROOT / "data" / "pdfs"
MANIFEST = ROOT / "data" / "books.yaml"
EXTRAS = ROOT / "data" / "extras.yaml"

SITE = "https://ncert.nic.in/"
INDEX_URL = SITE + "textbook.php"
PDF_URL = SITE + "textbook/pdf/{name}"
USER_AGENT = "Mozilla/5.0 (ncert-mcp book downloader; one file at a time)"
DELAY_SECONDS = 1.5
RETRIES = 3

# Subject as the NCERT menu names it -> our code, and the English book title we want.
WANTED = {
    "Science": ("sci", "Curiosity"),
    "Mathematics": ("math", "Ganita Prakash"),
    "Social Science": ("sst", "Exploring Society India and Beyond"),
}
# Words that must appear in the first pages of every PDF of that book.
BOOK_MARKERS = {"sci": "curiosity", "math": "ganita prakash", "sst": "exploring society"}


@dataclass
class Book:
    cls: int
    subject: str
    book: str  # canonical book name we store
    menu_title: str  # title exactly as on the NCERT menu
    code: str  # e.g. "gegp2"
    part: int
    chapters: int  # how many chapters the menu says the book has
    files: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def find_books(html: str) -> list[Book]:
    """Read the class/subject -> book menu out of textbook.php's JavaScript."""
    block_re = re.compile(
        r'tclass\.value==(\d+)\)\s*&&\s*\(document\.test\.tsubject\.options\[sind\]\.text=="([^"]+)"\)\)\s*\{(.*?)\}',
        re.S,
    )
    books = []
    for cls, subject, body in block_re.findall(html):
        if cls not in ("6", "7") or subject not in WANTED:
            continue
        body = re.sub(r"//[^\n]*", "", body)  # drop commented-out (old) books
        texts = dict(re.findall(r'options\[(\d+)\]\.text="([^"]+)"', body))
        values = dict(re.findall(r'options\[(\d+)\]\.value="([^"]+)"', body))
        code_subj, english_title = WANTED[subject]
        for idx, title in texts.items():
            # English edition only: exact title, optionally followed by a part marker.
            m = re.fullmatch(re.escape(english_title) + r"(?:\s*-?\s*(?:Part-?)?\s*(I{1,3}))?", title.strip())
            if not m or idx not in values:
                continue
            q = re.fullmatch(r"textbook\.php\?([a-z]+\d)=0-(\d+)", values[idx])
            if not q:
                continue
            code, count = q.group(1), int(q.group(2))
            part = int(code[-1])
            if m.group(1) and len(m.group(1)) != part:
                raise SystemExit(f"Part mismatch for {title!r}: code {code}")
            books.append(
                Book(
                    cls=int(cls),
                    subject=code_subj,
                    book=english_title.replace("Exploring Society India", "Exploring Society: India"),
                    menu_title=title,
                    code=code,
                    part=part,
                    chapters=count,
                )
            )
    return sorted(books, key=lambda b: (b.cls, b.subject, b.part))


def find_extras(html: str, codes: set[str]) -> dict[str, list[tuple[str, str]]]:
    """Extra book files the site lists for a code (e.g. Glossary = <code>gl.pdf), read from its JavaScript."""
    pat = re.compile(
        r'if\s*\(([^{}]*?)\)\s*\{\s*document\.write\("[^"]*?<span class=\\"sty1\\" >([^<]+)</span'
        r'[^"]*?textbook\.php\?" \+ pm \+ "=([a-z0-9]{2})-"',
        re.S,
    )
    out: dict[str, list[tuple[str, str]]] = {}
    for cond, label, ss in pat.findall(html):
        for code in set(re.findall(r'pm=="([a-z0-9]+)"', cond)) & codes:
            out.setdefault(code, []).append((label.strip(), ss))
    return out


def download(name: str, dest: Path) -> str | None:
    """Download one PDF politely. Returns an error message, or None on success."""
    if dest.exists() and looks_like_pdf(dest):
        return None
    last = "unknown error"
    for attempt in range(1, RETRIES + 1):
        time.sleep(DELAY_SECONDS)
        try:
            data = fetch(PDF_URL.format(name=name))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code == 404:
                return last  # not on the site; retrying won't help
        except Exception as e:  # network trouble: retry
            last = f"{type(e).__name__}: {e}"
        else:
            if not data.startswith(b"%PDF"):
                last = "not a PDF (probably an HTML error page)"
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.rename(dest)
                return None
        print(f"    retry {attempt}/{RETRIES} for {name}: {last}", file=sys.stderr)
    return last


def looks_like_pdf(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(4) == b"%PDF"


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def chapter_title(doc: pymupdf.Document) -> str:
    """Biggest real words on page 1 = the chapter title (the big chapter number is skipped)."""
    spans = []
    for block in doc[0].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for s in line["spans"]:
                if len(re.findall(r"[A-Za-z]", s["text"])) >= 3:
                    spans.append(s)
    if not spans:
        return ""
    top = max(s["size"] for s in spans)
    parts = [s for s in spans if s["size"] >= top - 1]
    parts.sort(key=lambda s: (round(s["bbox"][1]), s["bbox"][0]))
    title = re.sub(r"\s+", " ", " ".join(s["text"] for s in parts)).strip()
    title = unicodedata.normalize("NFKC", title)
    if title.isupper():
        small = {"a", "an", "and", "the", "of", "in", "on", "to", "for", "with", "at", "by"}
        words = title.lower().split()
        title = " ".join(w if (i and w in small) else w[:1].upper() + w[1:] for i, w in enumerate(words))
    return title


STOP_LINE = re.compile(
    r"(?i)^(theme\b|unit\b|glossary|learning material|political map|physical map|maps? of india|images and maps|"
    r"reprint|appendix|answers|index|references|foreword|about th|letter to|your journey|note to|a note|introduction)"
)


def parse_contents(doc: pymupdf.Document) -> dict[int, dict]:
    """Read the Contents page of a prelims PDF -> {chapter number in this part: {title, page}}."""
    text = ""
    for page in doc:
        t = page.get_text()
        if re.search(r"(?im)^\s*contents\s*$", t):
            text = t
            break
    out: dict[int, dict] = {}
    cur: dict | None = None
    for raw in text.split("\n"):
        line = unicodedata.normalize("NFKC", raw).replace("\t", " ").strip()
        if not line:
            continue
        m = re.match(r"^(?:chapter\s+(\d{1,2})|(\d{1,2})\.)\s*(.*)$", line, re.I)
        if m:
            cur = {"no": int(m.group(1) or m.group(2)), "parts": [], "page": None}
            out[cur["no"]] = cur
            line = m.group(3).strip()
            if not line:
                continue
        if cur is None:
            continue
        if STOP_LINE.match(line):
            cur = None
            continue
        if re.fullmatch(r"\d{1,3}", line):
            if cur["page"] is None:
                cur["page"] = int(line)
            continue
        cur["parts"].append(line)
    result = {}
    for no, c in out.items():
        title = re.sub(r"\s+", " ", " ".join(c["parts"])).strip()
        title = re.sub(r"\s+([,:])", r"\1", title)
        title = re.sub(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2060\ufeff]", "", title)
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            result[no] = {"title": title, "page": c["page"]}
    return result


def check_pdf(path: Path, book: Book) -> tuple[str | None, str]:
    """Returns (problem or None, chapter title from the first page)."""
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        return f"does not open: {e}", ""
    if doc.page_count == 0:
        return "has no pages", ""
    full = norm(" ".join(p.get_text() for p in doc))
    if BOOK_MARKERS[book.subject] not in full and not path.name.endswith("ps.pdf"):
        # Running headers name the book; a chapter with none at all is suspicious.
        return f"never mentions '{BOOK_MARKERS[book.subject]}'", ""
    if path.name.endswith("ps.pdf") and BOOK_MARKERS[book.subject] not in full:
        return f"prelims never mention '{BOOK_MARKERS[book.subject]}'", ""
    return None, chapter_title(doc)


def run(check_only: bool) -> int:
    print(f"Reading book list from {INDEX_URL}")
    html = fetch(INDEX_URL).decode("utf-8", errors="replace")
    books = find_books(html)
    extras_by_code = find_extras(html, {b.code for b in books})
    extras = []
    expected = {(c, s) for c in (6, 7) for s in ("sci", "math", "sst")}
    found = {(b.cls, b.subject) for b in books}
    for cls, subj in sorted(expected - found):
        print(f"WARNING: no English book found on the site for class {cls} {subj}")

    entries = []
    for book in books:
        folder = PDF_DIR / str(book.cls) / book.subject
        print(f"\nClass {book.cls} {book.menu_title} (code {book.code}, {book.chapters} chapters)")
        names = [f"{book.code}ps.pdf"] + [f"{book.code}{n:02d}.pdf" for n in range(1, book.chapters + 1)]
        for label, ss in extras_by_code.get(book.code, []):
            name = f"{book.code}{ss}.pdf"
            dest = folder / name
            err = None if check_only else download(name, dest)
            if err or not dest.exists():
                book.problems.append(f"{name} ({label}): {err or 'missing'}")
                continue
            extras.append({"file": f"pdfs/{book.cls}/{book.subject}/{name}", "class": book.cls, "subject": book.subject,
                           "book": book.book, "part": book.part, "kind": label.lower()})
            print(f"  ok     {name} ({label})")
        for name in names:
            dest = folder / name
            if not check_only:
                err = download(name, dest)
                if err:
                    book.problems.append(f"{name}: {err}")
                    print(f"  FAILED {name}: {err}")
                    continue
            if not dest.exists():
                book.problems.append(f"{name}: missing")
                continue
            problem, title = check_pdf(dest, book)
            if problem:
                book.problems.append(f"{name}: {problem}")
                print(f"  BAD    {name}: {problem}")
                continue
            if name.endswith("ps.pdf"):
                book.files.append({"name": name, "prelims": True})
                print(f"  ok     {name} (prelims)")
            else:
                book.files.append({"name": name, "local_no": int(name[-6:-4]), "title": title})
                print(f"  ok     {name}  {title}")

    # Chapter numbers run on across parts, the way students count them.
    for cls in (6, 7):
        for subj in ("sci", "math", "sst"):
            offset = 0
            for book in [b for b in books if b.cls == cls and b.subject == subj]:
                prelims = next((f for f in book.files if f.get("prelims")), None)
                contents = {}
                if prelims:
                    contents = parse_contents(pymupdf.open(PDF_DIR / str(cls) / subj / prelims["name"]))
                    if len(contents) != book.chapters:
                        book.problems.append(f"contents page lists {len(contents)} chapters, site says {book.chapters}")
                for f in book.files:
                    if f.get("prelims"):
                        continue
                    toc = contents.get(f["local_no"], {})
                    entry = {
                        "file": f"pdfs/{cls}/{subj}/{f['name']}",
                        "class": cls,
                        "subject": subj,
                        "book": book.book,
                        "part": book.part,
                        "chapter_no": offset + f["local_no"],
                        "chapter_in_part": f["local_no"],
                        "title": (toc.get("title") or f["title"]).strip(),
                        "book_page_start": toc.get("page"),
                    }
                    # The title from the contents must also be printed inside the chapter PDF itself.
                    pdf_text = norm(" ".join(p.get_text() for p in pymupdf.open(PDF_DIR / str(cls) / subj / f["name"])))
                    if not toc:
                        entry["title_check"] = "chapter not found on contents page; title read from page 1, verify by hand"
                    elif norm(toc["title"]) not in pdf_text:
                        entry["title_check"] = f"contents title not found in the PDF (page 1 says {f['title']!r}); verify"
                    entries.append(entry)
                offset += book.chapters

    header = (
        "# One entry per chapter PDF. Written by ingest/download_pdfs.py from ncert.nic.in.\n"
        "# chapter_no runs on across Part 1 / Part 2 (the way students count). 'part' is metadata only.\n"
        "# Titles were read from each chapter's first page. Fix any title by hand here if needed.\n"
    )
    MANIFEST.write_text(header + yaml.safe_dump(entries, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"\nWrote {MANIFEST.relative_to(ROOT)} with {len(entries)} chapters.")
    EXTRAS.write_text(
        "# Extra book files listed on ncert.nic.in (e.g. SST glossaries). Written by ingest/download_pdfs.py.\n"
        + yaml.safe_dump(extras, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"Wrote {EXTRAS.relative_to(ROOT)} with {len(extras)} extra files.")

    print("\nSUMMARY")
    print(f"{'Book':<52} {'Code':<7} {'Got':>4} {'Of':>4}  Problems")
    for b in books:
        got = sum(1 for f in b.files if not f.get("prelims"))
        has_ps = any(f.get("prelims") for f in b.files)
        label = f"Class {b.cls} {b.menu_title}"
        notes = "; ".join(b.problems) or ("none" if has_ps else "prelims missing")
        print(f"{label:<52} {b.code:<7} {got:>4} {b.chapters:>4}  {notes}")
    flagged = [e for e in entries if e.get("title_check")]
    for e in flagged:
        print(f"CHECK {e['file']}: {e['title_check']}")
    for cls, subj in sorted(expected - found):
        print(f"Class {cls} {subj}: NOT ON SITE (not released yet?)")
    return 1 if any(b.problems for b in books) or flagged else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="don't download, only check files on disk")
    sys.exit(run(ap.parse_args().check))
