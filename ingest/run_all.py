"""Run the whole Claude part of the ingestion, start to finish, with no manual steps.

  1. Maths: page images -> Claude -> Markdown with LaTeX (stage 1b), chapter by chapter
  2. All 83 chapters: Claude structuring (stage 2, --mode llm)
  3. Rebuild books.db, run the tests, write data/ingest_progress.json

Usage limits are handled in ingest/llm.py: when a limit is hit it sleeps until the reset and goes on.
Every finished page is saved in data/vision_cache/, and after every chapter the results are committed
and pushed, so if this machine goes away the next run continues where this one stopped.

    nohup python -m ingest.run_all > data/ingest_log.txt 2>&1 &
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime

from ingest import llm
from ingest.common import DATA, EXTRACTED, ROOT, STRUCTURED, load_book_config, load_manifest
from ingest.extract_vision import run_chapter
from ingest.structure import Doc, llm_mode

PROGRESS = DATA / "ingest_progress.json"
BRANCH = None  # current branch, read from git


def progress(**update) -> None:
    data = json.loads(PROGRESS.read_text()) if PROGRESS.exists() else {}
    data.update(update, updated_at=datetime.now().isoformat(timespec="seconds"), usage=dict(llm.last_usage))
    PROGRESS.write_text(json.dumps(data, indent=1))


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def save(message: str) -> None:
    """Commit and push the results so far (retries a few times on network trouble)."""
    git("add", "data/vision_cache", "data/extracted", "data/structured", "data/ingest_progress.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-q", "-m", f"{message}\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
    for wait in (0, 2, 4, 8, 16):
        time.sleep(wait)
        if git("push", "-q", "origin", f"HEAD:{BRANCH}").returncode == 0:
            return
    llm.log("push failed; results are committed locally and will go up with the next push")


def main() -> int:
    global BRANCH
    BRANCH = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    save("Maths vision pass: pages saved by an earlier run")  # pages left over from a stopped run
    try:
        os.remove(llm.RESUME_FILE)
    except FileNotFoundError:
        pass
    entries = load_manifest()
    maths = [e for e in entries if e["subject"] == "math"]
    cfg = load_book_config()
    llm.log(f"start: {len(maths)} maths chapters to read, {len(entries)} chapters to structure; {llm.describe()}")
    started = time.time()

    # 1. Maths vision pass. A chapter is done when its vision-check file exists.
    for n, e in enumerate(maths, 1):
        cid = e["chapter_id"]
        if (EXTRACTED / f"{cid}.vision-check.json").exists():
            continue
        t = time.time()
        rep = run_chapter(e, workers=3)
        flagged = f", check pages {rep['flagged_pages']}" if rep["flagged_pages"] else ""
        llm.log(f"vision {n}/{len(maths)} {cid}: {len(rep['pages'])} pages in {(time.time() - t) / 60:.0f} min{flagged}; usage {llm.last_usage}")
        progress(stage="vision", vision_done=n, vision_total=len(maths), last=cid)
        save(f"Maths vision pass: {cid} ({e['title']})")

    # 2. Claude structuring for every chapter (skips ones already done by the LLM after their latest text).
    for n, e in enumerate(entries, 1):
        cid = e["chapter_id"]
        src, out = EXTRACTED / f"{cid}.md", STRUCTURED / f"{cid}.json"
        if out.exists() and out.stat().st_mtime > src.stat().st_mtime:
            if json.loads(out.read_text()).get("structured_by", "").startswith("llm"):
                continue
        ch = llm_mode(e, Doc(src.read_text(encoding="utf-8")), cfg.get(e["subject"], {}))
        out.write_text(json.dumps(ch.dump(), indent=1, ensure_ascii=False), encoding="utf-8")
        warn = f" warnings: {'; '.join(ch.warnings[:3])}" if ch.warnings else ""
        llm.log(f"structure {n}/{len(entries)} {cid}: {len(ch.sections)} sections, {len(ch.exercises)} exercises{warn}")
        progress(stage="structure", structure_done=n, structure_total=len(entries), last=cid)
        if n % 5 == 0 or n == len(entries):
            save(f"Claude structuring: up to {cid}")

    # 3. Rebuild the database and test it.
    build = subprocess.run([sys.executable, "-m", "ingest.build_db"], cwd=ROOT, capture_output=True, text=True)
    tests = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True)
    summary = tests.stdout.strip().splitlines()[-1] if tests.stdout.strip() else tests.stderr[-300:]
    llm.log(f"books.db rebuilt: {build.stdout.strip().splitlines()[0] if build.stdout else build.stderr[-300:]}")
    llm.log(f"tests: {summary}")
    progress(stage="done", tests=summary, hours=round((time.time() - started) / 3600, 1))
    git("add", "data/books.db")
    save("Rebuild books.db after the Claude ingestion pass")
    return 0 if tests.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
