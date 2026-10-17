"""Recover letters that a PDF stores without a Unicode mapping.

Some NCERT PDFs embed a font subset with no ToUnicode entries for a few glyphs, so text extraction
returns U+FFFD ("�") instead of letters like W, y, u, k ("�hich ... do �o� thin�"). The glyph ids in
those subsets match the full copy of the same font (e.g. NotoSerif-Regular), so the full font's own
cmap tells us which letter each glyph draws. Full fonts are taken from this PDF first, then from any
other book PDF (all books use the same Noto fonts).
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pymupdf

from ingest.common import DATA, EXTRACTED

CACHE = EXTRACTED / ".cache" / "glyphmaps.json"
FULL_FONT_MIN_GLYPHS = 1000  # only trust complete fonts: subsets renumber their glyphs


def _font_map(doc: pymupdf.Document, xref: int) -> dict[int, str] | None:
    from fontTools.ttLib import TTFont

    try:
        _, _, _, buf = doc.extract_font(xref)
        if not buf:
            return None
        tt = TTFont(io.BytesIO(buf), lazy=True)
        if "cmap" not in tt:
            return None
        rev: dict[int, str] = {}
        for table in tt["cmap"].tables:
            if table.isUnicode():
                for u, name in table.cmap.items():
                    rev.setdefault(tt.getGlyphID(name), chr(u))
        return rev if len(rev) >= FULL_FONT_MIN_GLYPHS else None
    except Exception:
        return None


def _doc_maps(doc: pymupdf.Document) -> dict[str, dict[int, str]]:
    maps: dict[str, dict[int, str]] = {}
    seen = set()
    for page in doc:
        for f in page.get_fonts(full=True):
            xref, name = f[0], f[3]
            base = name.split("+")[-1]
            if xref in seen or base in maps:
                continue
            seen.add(xref)
            m = _font_map(doc, xref)
            if m:
                maps[base] = m
    return maps


def global_maps() -> dict[str, dict[int, str]]:
    """Full-font glyph maps from every downloaded book PDF (cached on disk)."""
    if CACHE.exists():
        raw = json.loads(CACHE.read_text())
        return {k: {int(g): c for g, c in v.items()} for k, v in raw.items()}
    maps: dict[str, dict[int, str]] = {}
    for pdf in sorted((DATA / "pdfs").glob("*/*/*.pdf")):
        for base, m in _doc_maps(pymupdf.open(pdf)).items():
            maps.setdefault(base, m)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(maps))
    return maps


class GlyphRepair:
    def __init__(self, doc: pymupdf.Document):
        self.maps = _doc_maps(doc)
        self._global: dict | None = None
        self.fixed = 0
        self.unknown = 0

    def _lookup(self, font: str, gid: int) -> str | None:
        base = font.split("+")[-1]
        m = self.maps.get(base)
        if m is None:
            if self._global is None:
                self._global = global_maps()
            m = self._global.get(base)
        ch = m.get(gid) if m else None
        return ch if ch and ch.isprintable() and ch != "�" else None

    def fix_page(self, page: pymupdf.Page, blocks: list[dict]) -> None:
        """Replace U+FFFD chars in a rawdict page in place, and fill in each span's 'text'."""
        index = None
        for b in blocks:
            for line in b.get("lines", []):
                for span in line["spans"]:
                    chars = span.get("chars", [])
                    if any(c["c"] == "�" for c in chars):
                        if index is None:
                            index = {}
                            for tspan in page.get_texttrace():
                                for uni, gid, origin, _bbox in tspan["chars"]:
                                    if uni == 0xFFFD or uni < 32:
                                        index[(round(origin[0], 1), round(origin[1], 1))] = (tspan["font"], gid)
                        for c in chars:
                            if c["c"] != "�":
                                continue
                            hit = index.get((round(c["origin"][0], 1), round(c["origin"][1], 1)))
                            ch = self._lookup(*hit) if hit else None
                            if ch:
                                c["c"] = ch
                                self.fixed += 1
                            else:
                                self.unknown += 1  # usually a bullet/icon glyph from a symbol font
                    span["text"] = "".join(c["c"] for c in chars)
