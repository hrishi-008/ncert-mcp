"""find_chapter against the real books.db: the tricky inputs from plan section 9."""

import pytest


def top(books, query, class_=None, subject=None):
    res = books.find_chapter(query, class_, subject)
    return res, [m.get("chapter_id") for m in res["matches"]]


@pytest.mark.parametrize("query,expected", [
    ("Class 7 science chapter 5", "7-sci-05"),
    ("VII sci ch5", "7-sci-05"),
    ("std 6 maths 3rd chapter", "6-math-03"),
    ("class 6 sst chapter 1", "6-sst-01"),
    ("class 7 maths chapter 9", "7-math-09"),      # Part 2, chapter 1 -> continuous numbering
    ("class 7 sst chapter 13", "7-sst-13"),        # Part 2, chapter 1
    ("class 7 social science last chapter", "7-sst-20"),
])
def test_exact_numbers(books, query, expected):
    res, ids = top(books, query)
    assert ids[0] == expected
    assert res["matches"][0]["confidence"] == "high"


@pytest.mark.parametrize("query,expected", [
    ("Exploring Magnts", "6-sci-04"),               # typo
    ("mindful eating", "6-sci-03"),
    ("the gupta era", "7-sst-07"),
    ("Grassroots Democracy local government in rural areas", "6-sst-11"),
    ("prime time", "6-math-05"),
])
def test_titles(books, query, expected):
    res, ids = top(books, query)
    assert ids[0] == expected, res
    assert res["matches"][0]["confidence"] == "high"


@pytest.mark.parametrize("query,class_,expected", [
    ("the magnets one", None, "6-sci-04"),
    ("magnet wala chapter", None, "6-sci-04"),
    ("the plants chapter", 7, "7-sci-10"),
    ("integers", 7, "7-math-10"),
])
def test_nicknames(books, query, class_, expected):
    res, ids = top(books, query, class_)
    assert expected in ids[:2], res


@pytest.mark.parametrize("old,expected", [
    ("Nutrition in Plants", "7-sci-10"),
    ("Fun with Magnets", "6-sci-04"),
    ("Simple Equations", "7-math-15"),
    ("Knowing Our Numbers", "7-math-01"),
    ("In the Earliest Cities", "6-sst-06"),
    ("Panchayati Raj", "6-sst-11"),
    ("acids bases and salts", "7-sci-02"),
])
def test_old_chapter_names(books, old, expected):
    res, ids = top(books, old)
    assert ids[0] == expected, res
    m = res["matches"][0]
    assert m["confidence"] == "high"
    assert "old" in m["match_reason"]
    assert "tell_teacher" in m and "now" in m["tell_teacher"]


@pytest.mark.parametrize("topic,class_,subject,expected", [
    ("photosynthesis", None, None, "7-sci-10"),
    ("fractions", 6, "math", "6-math-07"),
    ("Harappan civilisation", None, None, "6-sst-06"),
    ("latitude", None, None, "6-sst-01"),
    ("barter system", None, None, "7-sst-11"),
    ("monsoon", 7, "sst", "7-sst-03"),
])
def test_topics(books, topic, class_, subject, expected):
    res, ids = top(books, topic, class_, subject)
    assert expected in ids[:2], res
    assert res["matches"][ids.index(expected)]["confidence"] in ("high", "medium")


@pytest.mark.parametrize("query", [
    "trigonometric ratios sine cosine",
    "mitochondria and golgi bodies",
    "quadratic equations discriminant",
])
def test_not_in_syllabus(books, query):
    res, ids = top(books, query)
    assert all(m["confidence"] == "low" for m in res["matches"]), res
    assert any("not be in the syllabus" in n for n in res["notes"])


def test_wrong_class_suggests_right_class(books):
    res, ids = top(books, "photosynthesis", 6, "sci")
    assert "7-sci-10" in ids
    assert any("Class 7" in n for n in res["notes"])


def test_bad_input_is_helpful(books):
    from server.normalize import NormalizeError
    with pytest.raises(NormalizeError):
        books.find_chapter("the", None, None)
    with pytest.raises(LookupError, match="chapters 1-"):
        books.find_chapter("class 6 science chapter 40")
