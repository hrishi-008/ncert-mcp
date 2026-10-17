# CLAUDE.md: NCERT Question Paper MCP

Read this first. The full build plan is in `ncert-mcp-plan.md`. Follow it.

## What we are building (the gist)

My mom runs tuition classes for **CBSE Class 6 and 7** students. She makes question papers and worksheets for **Science, SST (Social Science) and Maths**. She is not technical.

She will use a **claude.ai Project**. In that project she sends:
- a photo of an old worksheet ("make a new one like this"),
- a short request ("Class 7 Science, Ch 3, 20 marks, 5 MCQ"), or
- a change ("make it easier", "change the values", "swap chapter 4 for 5").

Claude then makes a printable paper + a separate answer key.

**The problem:** Claude must not guess. Every question must come from what the **newest NCERT textbook** actually teaches. Chapter names and numbers changed in the new books, so Claude often gets confused.

**What this repo builds:** a **remote MCP server** (Python + FastMCP, Streamable HTTP) that the claude.ai Project connects to. It gives Claude:
1. **The textbooks:** find the right chapter from any input (number, title, nickname, old chapter name, topic), get chapter content, exercises, key terms, figures, and search across books.
2. **Teacher notes:** my mom's preferences and corrections. Claude reads and updates these as it learns her style. Full history is kept, and nothing is ever hard-deleted.
3. **Paper log:** saves each finished paper. Past questions are only checked when she asks for no repeats.

**Guiding rule: hassle-free.** Mom just chats. Nothing should ever need fixing by hand. Forgiving inputs, helpful errors, few tool calls.

## The books (newest NCERT editions ONLY)

| Subject | Book | Classes |
|---|---|---|
| Science | *Curiosity* | 6, 7 |
| Maths | *Ganita Prakash* | 6, 7 |
| SST | *Exploring Society: India and Beyond* | 6, 7 |

- **Never** use the old NCERT books (old Science, old *Mathematics*, *Our Pasts*, *The Earth Our Habitat*, *Social and Political Life*, etc.) as content. They are only used to build the old→new chapter map.
- If a book comes in **Part 1 and Part 2**, treat it as **one book with continuous chapter numbers**. Keep `part` as metadata only.

## Key decisions (already made, don't change without asking)

- Python 3.12, FastMCP, Streamable HTTP.
- Auth: secret URL `https://ncert.hrishk.com/<SECRET_TOKEN>/mcp`. Token in env var. All other paths → 404.
- Hosting: Hetzner VPS (CX23 or CAX11) + Caddy for HTTPS. Domain `hrishk.com` is already owned.
- Two databases: `books.db` (read-only, built offline, shipped with the app) and `state.db` (notes + paper log, read-write, must persist, backed up daily).
- Search: SQLite FTS5. No vector DB unless tests prove it's needed.
- Ingestion: pymupdf for Science and SST. **Vision LLM pass (Claude API) for Maths**, since equations break with plain text extraction.
- Full chapter structure from day one: sections, key terms, activities, figures, exercises, summary, topic tags.
- The LLM structuring step must **copy** source text, never rewrite it. Verify with a substring check.
- 9 MCP tools: `find_chapter`, `list_chapters`, `get_chapter_pack`, `get_section`, `search_books`, `get_teacher_notes`, `update_teacher_notes`, `log_paper`, `get_past_questions`. Restoring old notes versions is admin-only (CLI), not a tool.
- No LLM calls at runtime. The server only serves data.

## FIRST TASK: download all the textbook PDFs

Get every chapter PDF for the 6 books from the **official NCERT website only**: https://ncert.nic.in/textbook.php

Steps:
1. Open the NCERT textbook page. For **Class 6** and **Class 7**, find these books: *Curiosity* (Science), *Ganita Prakash* (Maths), *Exploring Society: India and Beyond* (SST). Include **all parts** if a book has Part 1 / Part 2. **English medium only.**
2. Work out each book's code and chapter PDF URLs from the site itself. NCERT serves chapter PDFs at URLs like `https://ncert.nic.in/textbook/pdf/<bookcode><chapter>.pdf`, and some books also offer a full-book zip. **Do not hardcode or guess book codes.** Read them from the page / its links, and confirm each code matches the right book title.
3. Also download each book's **prelims / contents PDF** (often the `ps` file). We need its table of contents to verify chapter numbers and titles.
4. Save to `data/pdfs/{class}/{subject}/` with the original NCERT filenames (`subject` = `sci`, `math`, `sst`).
5. Check every file: it opens, it isn't an HTML error page saved as `.pdf`, and the first page matches the expected book and chapter.
6. Create `data/books.yaml` with one entry per chapter: `file, class, subject, book, part, chapter_no` (continuous across parts), and `title` (read from the PDF / contents page).
7. Print a summary table: book → number of chapters downloaded → any missing or failed chapters.

Rules:
- Official `ncert.nic.in` only. No mirrors, no third-party sites.
- Be polite to the server: download one file at a time with a short delay, and retry failed ones up to 3 times.
- If the site blocks scripted downloads, stop and tell me. I'll download manually and drop the files in the folder.
- Add `data/pdfs/` to `.gitignore`.
- If you find a book is not yet released (for example, a Part 2 missing on the site), note it in the summary. Don't substitute an old book.

After the PDFs are in, continue with the build order in `ncert-mcp-plan.md` (section 10), starting with step 1 (skeleton + deploy).

## How to re-download the books

`python -m ingest.download_pdfs` reads the book codes from ncert.nic.in, downloads anything missing,
checks every file, and rewrites `data/books.yaml`. `--check` skips the download.
