import pytest

from ingest.cleanup import clean_chapter, clean_text, fix_heading
from ingest.schema import Chapter, Exercise, Section


def chapter(sections, exercises=()):
    return Chapter(
        chapter_id="7-sci-10", **{"class": 7}, subject="sci", book="Curiosity", part=1, chapter_no=10,
        title="Life Processes in Plants", sections=sections, exercises=list(exercises),
    )


def test_margin_icons_are_dropped():
    text = "1. Complete the table\n\n> ENQUIRY\n\nRaw materials\n\n> ?\n\n> W HAT\n\n> [Figure: Math Talk icon]\n\nProducts"
    assert clean_text(text) == "1. Complete the table\n\nRaw materials\n\nProducts"


@pytest.mark.parametrize("label", ["> N", "> E W", "> INDIA", "> ARABIAN", "> Iron nail", "> Math Talk"])
def test_map_and_figure_labels_are_kept(label):
    assert clean_text(f"Map\n\n{label}") == f"Map\n\n{label}"


@pytest.mark.parametrize("raw,clean", [
    ("10.3 Transport in Plants p", "10.3 Transport in Plants"),
    ("### 1.1 Happy Exploring! g ppy p", "### 1.1 Happy Exploring!"),
    ("### 4.5 Are Non-metals Essential in Everyday Life? y y", "### 4.5 Are Non-metals Essential in Everyday Life?"),
    ("10.4 Do Plants Respire?", "10.4 Do Plants Respire?"),
    ("Keywords", "Keywords"),
])
def test_heading_debris(raw, clean):
    assert fix_heading(raw) == clean


def test_orphan_heading_moves_to_the_section_it_introduces():
    ch = clean_chapter(chapter([
        Section(section_id="s07", heading="10.2.4 Gases", text="#### 10.2.4 Gases\n\nStomata.\n\n### 10.3 Transport in Plants p\n"),
        Section(section_id="s08", heading="10.3.1 Water", text="#### 10.3.1 Water\n\nRoots."),
    ]))
    assert ch.sections[0].text == "#### 10.2.4 Gases\n\nStomata."
    assert ch.sections[1].text == "### 10.3 Transport in Plants\n\n#### 10.3.1 Water\n\nRoots."


def test_unnumbered_trailing_heading_stays():
    ch = clean_chapter(chapter([
        Section(section_id="s01", heading="Intro", text="Body.\n\n##### know!"),
        Section(section_id="s02", heading="3.5 Millets", text="### 3.5 Millets\n\nText."),
    ]))
    assert ch.sections[0].text == "Body.\n\n##### know!"


def test_exercises_are_cleaned():
    ch = clean_chapter(chapter([Section(section_id="s01", heading="Intro", text="x")],
                               [Exercise(label="Q1", text="1. Explain.\n\n> CURIOSITY")]))
    assert ch.exercises[0].text == "1. Explain."
