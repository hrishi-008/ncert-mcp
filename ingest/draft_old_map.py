"""Draft the old -> new chapter map with Claude, for HK to review by hand (~30 min).

Input:  data/old_toc.yaml (old NCERT chapter names) + the new chapters (titles, topics, summaries from
        data/structured/*.json).
Output: data/old_to_new_map.draft.json. Review it, fix it, then save it as data/old_to_new_map.json
        and rebuild books.db (python -m ingest.build_db).

One old chapter may map to several new ones (any class/subject), or to none ("not in new syllabus").

Usage (needs ANTHROPIC_API_KEY):
    python -m ingest.draft_old_map
"""

from __future__ import annotations

import json
import sys

import yaml

from ingest import llm
from ingest.common import DATA, STRUCTURED

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entries"],
    "properties": {"entries": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["old_title", "old_class", "old_subject", "new_chapter_ids", "confidence", "note"],
        "properties": {
            "old_title": {"type": "string"},
            "old_class": {"type": "integer"},
            "old_subject": {"type": "string", "enum": ["sci", "math", "sst"]},
            "new_chapter_ids": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "note": {"type": "string", "description": "one short line for the teacher, e.g. 'now split across two chapters'"},
        }}}},
}

SYSTEM = """You map chapters of the OLD NCERT Class 6-7 books to the NEW NCERT books (Curiosity, Ganita Prakash,
Exploring Society: India and Beyond). For every old chapter, list the new chapter_ids that now teach most of
the same content (any class: topics moved between Class 6 and 7). Use [] if the content is not in the new
books at all. Only use chapter_ids from the list given. Base it on the new chapters' topics and summaries,
not on guesses. Mark confidence honestly; HK reviews every row."""


def main() -> int:
    old = yaml.safe_load((DATA / "old_toc.yaml").read_text(encoding="utf-8"))
    new = []
    for f in sorted(STRUCTURED.glob("*.json")):
        ch = json.loads(f.read_text(encoding="utf-8"))
        new.append({"chapter_id": ch["chapter_id"], "title": ch["title"], "topics": ch["topics"][:20],
                    "summary": ch["summary"][:600], "sections": [s["heading"] for s in ch["sections"]][:25]})
    if not new:
        print("No structured chapters yet: run ingest.structure first.")
        return 1
    c = llm.client()
    resp = llm.create(
        c, model=llm.MODEL, max_tokens=32000, system=SYSTEM,
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": "OLD chapters:\n" + yaml.safe_dump(old, allow_unicode=True)
                   + "\n\nNEW chapters:\n" + json.dumps(new, ensure_ascii=False, indent=0)}],
    )
    data = json.loads(llm.text_of(resp))
    valid = {n["chapter_id"] for n in new}
    for e in data["entries"]:
        bad = [i for i in e["new_chapter_ids"] if i not in valid]
        if bad:
            e["new_chapter_ids"] = [i for i in e["new_chapter_ids"] if i in valid]
            e["note"] = (e["note"] + f" [dropped unknown ids {bad}]").strip()
    out = DATA / "old_to_new_map.draft.json"
    out.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    low = sum(e["confidence"] != "high" for e in data["entries"])
    print(f"Wrote {out} with {len(data['entries'])} rows ({low} not high-confidence). Review, then save as data/old_to_new_map.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
