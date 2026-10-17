"""Books + tools on the real books.db."""

import pytest

from server.tools import PACK_CHAR_BUDGET, _format_pack


def all_ids(books):
    return [r["chapter_id"] for r in books.db.execute("SELECT chapter_id FROM chapters")]


def test_all_83_chapters_loaded(books):
    ids = all_ids(books)
    assert len(ids) == 83
    counts = books.stats()["chapters"]
    assert counts == {"6-math": 10, "6-sci": 12, "6-sst": 14, "7-math": 15, "7-sci": 12, "7-sst": 20}


def test_list_chapters_parts_run_on(books):
    res = books.list_chapters("VII", "maths")
    assert [c["chapter_no"] for c in res["chapters"]] == list(range(1, 16))
    assert res["chapters"][8]["part"] == 2 and res["chapters"][8]["title"] == "Geometric Twins"


def test_every_pack_is_small_and_complete(books):
    for cid in all_ids(books):
        p = books.chapter_pack(cid)
        text = _format_pack(p, "_No teacher notes saved yet._")
        assert len(text) <= PACK_CHAR_BUDGET + 200, cid
        assert p["sections"], cid
        assert "Teacher notes" in text


def test_pack_has_exercises_for_most_chapters(books):
    without = [cid for cid in all_ids(books) if not books.chapter_pack(cid)["exercises"]]
    assert len(without) <= 5, without


def test_sections_reassemble_the_whole_chapter(books):
    """No text is lost between extraction and the database."""
    from pathlib import Path
    from ingest.common import squash
    root = Path(__file__).resolve().parent.parent / "data" / "extracted"
    for cid in all_ids(books):
        md = (root / f"{cid}.md").read_text(encoding="utf-8")
        secs = books.db.execute("SELECT text FROM sections WHERE chapter_id=? ORDER BY ord", (cid,)).fetchall()
        assert squash("\n".join(s["text"] for s in secs)) == squash(md), cid


def test_get_section_forgiving(books):
    s1 = books.get_section("6-sci-04", "6-sci-04-s02")
    assert books.get_section("6-sci-04", "2")["section_id"] == s1["section_id"]
    assert books.get_section("6 sci 4", "s2")["section_id"] == s1["section_id"]
    with pytest.raises(LookupError, match="Sections are"):
        books.get_section("6-sci-04", "zzzz qqqq")


def test_search(books):
    res = books.search("photosynthesis", class_=7)
    assert res["results"] and res["results"][0]["chapter_id"] == "7-sci-10"
    assert res["results"][0]["page"]
    assert books.search("xylophonist quasar")["results"] == []


def test_unknown_chapter_is_helpful(books):
    with pytest.raises(LookupError, match="chapters 1-12"):
        books.chapter("6-sci-30")
