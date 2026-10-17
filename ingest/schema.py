"""The structured chapter format (stage 2 output, stage 4 input)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Section(BaseModel):
    section_id: str  # e.g. "7-sci-05-s03"
    heading: str
    text: str  # copied verbatim from the extracted Markdown, never rewritten
    page_start: int | None = None
    page_end: int | None = None


class KeyTerm(BaseModel):
    term: str
    meaning: str = ""
    page: int | None = None


class Activity(BaseModel):
    label: str  # e.g. "Activity 5.2"
    description: str
    page: int | None = None


class Figure(BaseModel):
    label: str  # e.g. "Fig. 5.3"
    caption: str
    page: int | None = None


class Exercise(BaseModel):
    label: str  # e.g. "Keep the curiosity alive Q4" or "Figure it Out 2.1 Q3"
    text: str
    type_hint: str = ""  # mcq / short / long / fill_blank / true_false / match / numerical / activity / other
    page: int | None = None
    source: str = "end"  # "end" = end-of-chapter exercises, "in_chapter" = a box inside the chapter


class Chapter(BaseModel):
    chapter_id: str
    class_: int = Field(alias="class")
    subject: str
    book: str
    part: int
    chapter_no: int
    title: str
    summary: str = ""
    sections: list[Section] = []
    key_terms: list[KeyTerm] = []
    activities: list[Activity] = []
    figures: list[Figure] = []
    exercises: list[Exercise] = []
    topics: list[str] = []
    page_start: int | None = None
    page_end: int | None = None
    structured_by: str = ""  # "llm:<model>" or "heuristic"
    warnings: list[str] = []

    model_config = {"populate_by_name": True}

    def dump(self) -> dict:
        return self.model_dump(by_alias=True)
