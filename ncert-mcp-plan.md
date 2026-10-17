# NCERT Question Paper MCP: Build Plan

A remote MCP server that gives Claude the new NCERT textbooks (Class 6 & 7: Science, SST, Maths), the teacher's notes, and a paper log. Used from a claude.ai Project by a non-technical tuition teacher. **Goal: hassle-free. She just chats, and nothing ever needs fixing by hand.**

---

## 1. Decisions (locked)

| Topic | Decision |
|---|---|
| Language / framework | Python 3.12 + FastMCP, Streamable HTTP transport |
| Domain | `ncert.hrishk.com` (subdomain of hrishk.com, A record → VPS IP) |
| Auth | Secret URL: `https://ncert.hrishk.com/<SECRET_TOKEN>/mcp`, token in env var |
| Books | Newest NCERT only: *Curiosity* (Sci), *Ganita Prakash* (Maths), *Exploring Society: India and Beyond* (SST), Class 6 & 7 |
| Source files | NCERT chapter-wise PDFs |
| Parts (Part 1 / Part 2) | Treated as **one book with continuous chapter numbers**, the way students refer to it. `part` kept only as metadata. |
| Structure extraction | **Full structure from day one:** sections, key terms, activities, figures, exercises, summary |
| Search | SQLite + FTS5 (BM25). No vector DB. Add embeddings only if tests show topic searches failing. |
| Teacher notes | Live in the MCP (replaces teacher-notes.md). Full history kept. |
| Repeated questions across papers | **Allowed by default.** Past questions are checked only when she explicitly asks for no repeats. |
| Notes / log viewer web page | Good to have. Last phase, optional. |
| Hosting | **Hetzner VPS (CX23 or CAX11) + Caddy** (decided; see CLAUDE.md and section 8) |

---

## 2. Architecture

```
claude.ai Project (teacher chats here)
          │  Streamable HTTP + secret URL
          ▼
FastMCP server
  ├── books.db    READ-ONLY, built offline, shipped with the app
  └── state.db    READ-WRITE: teacher notes + history + paper log
```

**Key idea:** keep books and state in **separate databases**.
- `books.db` is rebuilt offline by the ingestion pipeline and deployed with the code. It never changes at runtime.
- `state.db` holds what Claude writes (notes, paper log). It must live on **persistent storage** (see section 8, Hosting).

---

## 3. Folder layout

```
ncert-mcp/
├── pyproject.toml
├── .env.example              # SECRET_TOKEN, STATE_DB_URL, ANTHROPIC_API_KEY
├── data/
│   ├── pdfs/                 # raw NCERT PDFs (gitignored)
│   │   └── {class}/{subject}/*.pdf
│   ├── books.yaml            # manifest: which PDF = which class/subject/chapter
│   ├── old_to_new_map.json   # hand-reviewed old chapter → new chapter map
│   ├── extracted/            # stage 1 output: markdown per chapter (cached)
│   ├── structured/           # stage 2 output: JSON per chapter (cached)
│   └── books.db              # stage 4 output
├── ingest/
│   ├── download_pdfs.py      # stage 0: fetch PDFs from ncert.nic.in, write books.yaml
│   ├── extract_text.py       # stage 1a: pymupdf → markdown (Sci, SST)
│   ├── extract_vision.py     # stage 1b: page images → vision LLM → markdown (Maths)
│   ├── structure.py          # stage 2: LLM → structured JSON (pydantic-validated)
│   ├── review.py             # stage 3: prints a review report for spot checks
│   ├── build_db.py           # stage 4: JSON → books.db (+ FTS index)
│   └── draft_old_map.py      # LLM drafts old→new map for human review
├── server/
│   ├── main.py               # FastMCP app, secret-path mounting, rate limit
│   ├── books.py              # read-only queries on books.db
│   ├── state.py              # notes + paper log on state.db
│   ├── normalize.py          # "VII"/"class 7"/"sci"/"Curiosity" → canonical
│   └── tools.py              # tool definitions + descriptions
├── admin/
│   └── notes_cli.py          # list history, restore a version (NOT an MCP tool)
└── tests/
    ├── test_find_chapter.py  # 20-30 tricky inputs (see section 9)
    └── test_tools.py
```

---

## 4. Ingestion pipeline (run offline, once per book update)

**`books.yaml` manifest:** one entry per PDF.
```yaml
- file: pdfs/7/sci/gecu105.pdf
  class: 7
  subject: sci
  book: Curiosity
  part: 1
  chapter_no: 5        # continuous numbering across parts
  title: "..."         # filled from the PDF, verified by hand
```

**Stage 1: extract to Markdown (cached per chapter)**
- **Sci and SST** → `pymupdf`. Keep font size and bold info so headings and key terms survive. Mark page numbers (`<!-- page 47 -->`).
- **Maths** → render each page at ~200 DPI and send it to a vision model (Claude) with this prompt: *"Convert to clean Markdown. Use LaTeX ($...$) for all maths. Keep tables as Markdown tables. Keep boxes like 'Figure it Out' as headed blocks. Describe figures in one line as [Figure: ...]."* Cache by page hash so re-runs are free.

**Stage 2: structure (LLM, per chapter)**
Feed the chapter Markdown to an LLM with a strict JSON schema (pydantic):
```
Chapter {
  chapter_id, class, subject, book, part, chapter_no, title,
  summary,                       # 5-8 lines, plain English
  sections: [{section_id, heading, text, page_start}],
  key_terms: [{term, meaning, page}],
  activities: [{label, description, page}],
  figures: [{label, caption, page}],
  exercises: [{label, text, type_hint, page}],  # end-of-chapter + in-chapter boxes
  topics: [string]                # short topic tags for search ("photosynthesis")
}
```
- Section `text` must be **copied from the source, not rewritten.** The LLM only splits and labels. Validate this by checking that each section's text is a substring (after whitespace normalising) of the Markdown.
- Each book uses its own headings for exercises and in-chapter question boxes. Put the exact heading names in a per-book config after looking at the PDFs, and pass them to the prompt.

**Stage 3: review (human, ~1 hour total)**
`review.py` prints, per chapter: title, section count, exercise count, key-term count, figure count, and any warnings (0 exercises, very short sections, substring-check failures). Read 2-3 Maths chapters fully against the PDF.

**Stage 4: build `books.db`**
```sql
chapters(chapter_id PK, class, subject, book, part, chapter_no, title, summary, topics_json)
sections(section_id PK, chapter_id, ord, heading, text, page_start)
key_terms(chapter_id, term, meaning, page)
activities(chapter_id, label, description, page)
figures(chapter_id, label, caption, page)
exercises(chapter_id, label, text, type_hint, page)
old_map(old_title, old_class, old_subject, new_chapter_id, note)
-- FTS5 virtual tables:
fts_sections(text, heading, chapter_id UNINDEXED, section_id UNINDEXED)
fts_chapters(title, topics, summary, chapter_id UNINDEXED)
```

**Old → new map:** `draft_old_map.py` gives an LLM the old NCERT tables of contents and the new chapter titles + topics, and drafts `old_to_new_map.json`. **HK reviews it by hand** (~30 min). One old chapter may map to several new ones, or to "not in new syllabus".

**Chapter ID format:** `{class}-{subject}-{chapter_no:02d}`, e.g. `7-sci-05`, `6-math-11`, `7-sst-03`.

---

## 5. MCP tools (9)

Tool descriptions matter more than code. Write them as instructions to Claude: when to use the tool, and what comes back.

### Books

**1. `find_chapter(query, class_=None, subject=None)`**
Resolves anything to chapters: a number ("ch 5"), a title, a nickname ("the plants chapter"), an **old NCERT chapter name**, or a topic.
Order: exact number → title match → `old_map` → FTS over titles/topics/summary → FTS over sections.
Returns the top 3 with `chapter_id`, title, `match_reason` ("matched old chapter name 'X'"), and `confidence` (high/medium/low). If the match came from the old map, include a line Claude can pass on: *"This was 'X' in the old book; it is now Ch N 'Y'."*

**2. `list_chapters(class_, subject)`**
All chapters: number, title, one-line summary.

**3. `get_chapter_pack(chapter_id)`**: the main tool, one call per paper
Returns: title, summary, section list (heading + first ~2 lines each), key terms, activities, figures, exercises, **teacher notes**.
Does NOT return full section text (keeps the response small). Target under ~8k tokens.

**4. `get_section(chapter_id, section_id)`**
Full text of one section, with page numbers.

**5. `search_books(query, class_=None, subject=None, chapter_id=None, limit=8)`**
BM25 search over sections. Returns snippets with chapter, section and page. Used for "is this topic in the syllabus?" and for finding exact facts. If there are no hits, say so clearly, so Claude tells her it's not in the book.

### Teacher notes

**6. `get_teacher_notes()`**
Returns current notes as Markdown, grouped by section. (Also included in `get_chapter_pack`.)

**7. `update_teacher_notes(section, action, text, reason, note_id=None)`**
- `section`: one of `paper_preferences`, `question_types`, `format_layout`, `corrections`
- `action`: `add` | `replace` | `delete` (`replace`/`delete` need `note_id`)
- `reason`: short, e.g. "She removed true/false twice"
- Every call writes a history row. Nothing is ever hard-deleted.

**Restoring old versions is NOT an MCP tool.** Only HK can do it, via `admin/notes_cli.py`.

### Paper log

**8. `log_paper(class_, subject, chapter_ids, total_marks, questions)`**
Called after every finished paper. `questions` = list of `{section, type, marks, text}`.

**9. `get_past_questions(chapter_id, last_n_papers=5)`**
Description must say: *"Use ONLY when the teacher asks to avoid repeating earlier questions."*

### Input normalising (`normalize.py`)
- Class: `6`, `VI`, `class 6`, `std 6`, `6th` → `6`
- Subject: `sci`, `science`, `curiosity` → `sci`; `maths`, `math`, `ganita`, `ganita prakash` → `math`; `sst`, `social`, `social science`, `exploring society`, `history`/`geography`/`civics` → `sst`
- Bad inputs → a helpful error with suggestions, never a bare "not found".

---

## 6. state.db schema

```sql
notes(note_id PK, section, text, active, created_at, updated_at)
notes_history(id PK, note_id, action, old_text, new_text, reason, at)
papers(paper_id PK, class, subject, chapter_ids_json, total_marks, created_at)
paper_questions(paper_id, section, type, marks, text)
```
Seed `notes` as empty. Instructions in the Project cover defaults.

---

## 7. Server details

- FastMCP, Streamable HTTP, mounted at `/{SECRET_TOKEN}/mcp`. Any other path → 404 (don't reveal that the server exists).
- Simple in-memory rate limit (e.g. 60 req/min).
- `books.db` opened read-only (`?mode=ro`).
- Log tool name + duration + errors (no content) for debugging.
- `/{SECRET_TOKEN}/health` for uptime checks.

---

## 8. Hosting

**Decided: Hetzner VPS (CX23 or CAX11) + Caddy for HTTPS.** One box, nothing sleeps, `state.db` is a plain
SQLite file on its disk. (Earlier options considered: Render free sleeps and has no persistent disk; Railway
free credit is limited; free host + Turso/Neon adds moving parts.)

`state.db` must survive restarts and redeploys, or her notes get wiped. Keep all `state.db` access behind
one small interface (`state.py`), so swapping SQLite file ↔ Turso later is a one-file change.

**Back up `state.db` daily** (cron → copy to a private place). Her notes become valuable over time.

---

## 9. Testing (before she uses it)

`tests/test_find_chapter.py`, 20-30 cases, e.g.:
- Exact: "Class 7 science chapter 5"
- Roman numerals / slang: "VII sci ch5", "std 6 maths 3rd chapter"
- Title with typo
- Nickname: "the plants chapter", "magnets one"
- **Old chapter names** from the old NCERT books (several per subject)
- Topic: "photosynthesis", "fractions", "Harappan civilisation", "latitude"
- Not in syllabus: a Class 9 topic → must return low confidence / no match, not a wrong chapter
- Wrong class: a Class 7 topic asked under Class 6 → should suggest the right class

Then a **live dry run in claude.ai**: 5 real requests (one photo worksheet, one text request, one "change the values", one "make it easier", one correction that should update notes). Check that tool calls are sensible and that the notes update.

---

## 10. Build order (for Claude Code)

0. **Download all PDFs** from ncert.nic.in → `data/pdfs/`, write `books.yaml` (see CLAUDE.md, "FIRST TASK").
1. **Skeleton:** pyproject, FastMCP hello-world tool, secret path, deployed to the host, added as a connector in claude.ai. *(Prove the pipe works before building anything else.)*
2. `books.yaml` + **Stage 1a** (Sci/SST pymupdf) for ONE chapter → look at the output.
3. **Stage 2** structuring for that one chapter → check the JSON.
4. **Stage 4** build_db + tools 1-5 working on that one chapter.
5. **Stage 1b** Maths vision extraction for ONE chapter → check equations carefully.
6. Run the full pipeline on all chapters → **Stage 3** review report → fix.
7. Old→new map: draft → **HK review**.
8. `state.db` + tools 6-9 + `notes_cli.py`.
9. Tests (section 9) → fix `find_chapter` until they pass.
10. Update Project instructions (Appendix A), remove `teacher-notes.md` from Project files.
11. Live dry run → hand over to Mom.
12. *(Optional)* read-only notes/log viewer page.
