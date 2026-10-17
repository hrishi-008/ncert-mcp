import pytest

from server.normalize import NormalizeError, normalize_chapter_id, normalize_class, normalize_subject, parse_query


@pytest.mark.parametrize("raw", [6, "6", "VI", "vi", "class 6", "Class VI", "std 6", "6th", "grade 6", "sixth"])
def test_class_6(raw):
    assert normalize_class(raw) == 6


@pytest.mark.parametrize("raw", [7, "VII", "class 7", "std. 7", "7th", "Grade VII"])
def test_class_7(raw):
    assert normalize_class(raw) == 7


def test_class_out_of_range_is_helpful():
    with pytest.raises(NormalizeError, match="only have Class 6 and Class 7"):
        normalize_class("class 9")


@pytest.mark.parametrize("raw,code", [
    ("sci", "sci"), ("Science", "sci"), ("Curiosity", "sci"),
    ("maths", "math"), ("Math", "math"), ("ganita", "math"), ("Ganita Prakash", "math"),
    ("sst", "sst"), ("social", "sst"), ("Social Science", "sst"), ("Exploring Society", "sst"),
    ("history", "sst"), ("geography", "sst"), ("civics", "sst"),
])
def test_subjects(raw, code):
    assert normalize_subject(raw) == code


def test_bad_subject_suggests():
    with pytest.raises(NormalizeError, match="science"):
        normalize_subject("hindi")


def test_chapter_ids():
    assert normalize_chapter_id("7-sci-5") == "7-sci-05"
    assert normalize_chapter_id("7 SCI 05") == "7-sci-05"
    assert normalize_chapter_id("VI-maths-3") == "6-math-03"
    with pytest.raises(NormalizeError):
        normalize_chapter_id("chapter five")


@pytest.mark.parametrize("q,cls,subj,no", [
    ("Class 7 science chapter 5", 7, "sci", 5),
    ("VII sci ch5", 7, "sci", 5),
    ("std 6 maths 3rd chapter", 6, "math", 3),
    ("7 sci 5", 7, "sci", 5),
    ("grade vi ganita prakash chapter five", 6, "math", 5),
    ("class 7 sst lesson 13", 7, "sst", 13),
])
def test_parse_query(q, cls, subj, no):
    p = parse_query(q)
    assert (p["class"], p["subject"], p["chapter_no"]) == (cls, subj, no)
    assert p["rest"] == ""


def test_parse_query_keeps_topic_words():
    assert parse_query("the plants chapter")["rest"] == "the plants chapter"
    assert parse_query("class 6 history")["rest"] == "history"
