"""Stage 3: print a review report so a human can spot-check the pipeline (~1 hour for all books).

Per chapter: pages, word-completeness from stage 1, sections / exercises / key terms / figures from
stage 2, and every warning. Ends with a short list of what to read by hand.

Usage:
    python -m ingest.review                 # all chapters
    python -m ingest.review --subject math  # one subject
    python -m ingest.review --markdown data/review.md   # also write it to a file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ingest.common import EXTRACTED, STRUCTURED, load_manifest, select


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subject", action="append")
    ap.add_argument("--markdown", type=Path)
    args = ap.parse_args()
    rows, todo = [], []
    header = f"{'chapter':<10} {'pages':<9} {'words':>6} {'miss':>4} {'img':>3} {'sec':>3} {'ex':>3} {'terms':>5} {'figs':>4} {'by':<10} title"
    rows.append(header)
    rows.append("-" * len(header))
    for e in select(load_manifest(), None, args.subject):
        cid = e["chapter_id"]
        check_f, struct_f = EXTRACTED / f"{cid}.check.json", STRUCTURED / f"{cid}.json"
        vision_f = EXTRACTED / f"{cid}.vision-check.json"
        if not check_f.exists():
            rows.append(f"{cid:<10} NOT EXTRACTED")
            todo.append(f"{cid}: run python -m ingest.extract_text {cid}")
            continue
        chk = json.loads(check_f.read_text())
        st = json.loads(struct_f.read_text()) if struct_f.exists() else None
        pages = f"{chk['printed_pages'][0]}-{chk['printed_pages'][1]}"
        if st:
            by = "llm" if st["structured_by"].startswith("llm") else "heuristic"
            counts = f"{len(st['sections']):>3} {len(st['exercises']):>3} {len(st['key_terms']):>5} {len(st['figures']):>4} {by:<10}"
        else:
            counts = f"{'-':>3} {'-':>3} {'-':>5} {'-':>4} {'none':<10}"
            todo.append(f"{cid}: not structured yet (python -m ingest.structure {cid})")
        rows.append(f"{cid:<10} {pages:<9} {chk['words_expected']:>6} {chk['missing_words']:>4} "
                    f"{len(chk['image_only_pages']):>3} {counts} {e['title']}")
        if chk["missing_words"]:
            todo.append(f"{cid}: {chk['missing_words']} words missing from extraction: {chk['missing_sample'][:8]}")
        if chk["image_only_pages"]:
            todo.append(f"{cid}: pages {chk['image_only_pages']} have no text layer (pictures/maps only): look at them in the PDF")
        if e["subject"] == "math" and not vision_f.exists():
            todo.append(f"{cid}: maths still uses plain-text extraction; run the vision pass (needs API key)")
        if vision_f.exists():
            v = json.loads(vision_f.read_text())
            if v["flagged_pages"]:
                todo.append(f"{cid}: vision pass may have skipped text on pages {v['flagged_pages']}")
        for w in (st or {}).get("warnings", []):
            rows.append(f"{'':<10}   ! {w}")
    rows.append("")
    rows.append("TO CHECK BY HAND")
    rows += [f"- {t}" for t in todo] or ["- nothing flagged"]
    rows.append("- Read 2-3 Maths chapters fully against the PDF (equations, tables, 'Figure it Out' boxes).")
    text = "\n".join(rows)
    print(text)
    if args.markdown:
        args.markdown.write_text("```\n" + text + "\n```\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
