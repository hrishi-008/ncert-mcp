# NCERT Question Paper MCP

A remote MCP server for a claude.ai Project. It gives Claude the **new NCERT books** for CBSE
Class 6 and 7 (*Curiosity*, *Ganita Prakash*, *Exploring Society: India and Beyond*), the teacher's
notes, and a log of her papers. Background and decisions: [CLAUDE.md](CLAUDE.md),
[ncert-mcp-plan.md](ncert-mcp-plan.md).

```
claude.ai Project ──HTTPS──> Caddy ──> MCP server (/<SECRET_TOKEN>/mcp)
                                         ├── data/books.db   read-only, built offline, shipped in the image
                                         └── state/state.db  notes + paper log, on the VPS disk, backed up daily
```

## What's in the repo

| Path | What |
|---|---|
| `server/` | The MCP server: 9 tools, secret-path routing, rate limit, health check |
| `ingest/` | Offline pipeline: download PDFs → extract → structure → review → build `books.db` |
| `admin/notes_cli.py` | See note history, restore an old version (admin only, not an MCP tool) |
| `data/books.yaml` | One entry per chapter (written by the downloader from ncert.nic.in) |
| `data/extracted/` | Stage 1 output: every chapter as Markdown + a completeness report |
| `data/structured/` | Stage 2 output: every chapter as structured JSON |
| `data/books.db` | Stage 4 output, what the server reads |
| `data/old_to_new_map.json` | Old NCERT chapter names → new chapters (**draft, needs HK review**) |
| `deploy/` | Caddyfile and the daily `state.db` backup script |

## The 9 tools

`find_chapter`, `list_chapters`, `get_chapter_pack`, `get_section`, `search_books`,
`get_teacher_notes`, `update_teacher_notes`, `log_paper`, `get_past_questions`.
The server makes **no LLM calls**; it only serves data.

## Run it locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[ingest,dev]'
cp .env.example .env            # set SECRET_TOKEN (any long random string)
export $(grep -v '^#' .env | xargs)
STATE_DB_URL=sqlite:///data/state.db BOOKS_DB_PATH=data/books.db python -m server.main
# MCP endpoint: http://localhost:8000/<SECRET_TOKEN>/mcp   health: http://localhost:8000/<SECRET_TOKEN>/health
pytest
```

## Build the book data (offline)

```bash
python -m ingest.download_pdfs          # 1. PDFs from ncert.nic.in -> data/pdfs/, writes data/books.yaml
python -m ingest.extract_text --all     # 2. every chapter -> Markdown, checks no word is lost
python -m ingest.extract_vision --all   # 3. Maths only, needs ANTHROPIC_API_KEY: page images -> Markdown + LaTeX
python -m ingest.structure --all        # 4. Markdown -> structured JSON (uses the LLM if a key is set)
python -m ingest.review                 # 5. review report: read it, spot-check by hand
python -m ingest.draft_old_map          # 6. optional, needs key: draft old->new map, then HK reviews it
python -m ingest.build_db               # 7. -> data/books.db
```

Without an API key, steps 2, 4 (heuristic mode), 5 and 7 still work, and that is what the
committed `books.db` was built with. With a key, re-run 3 and `4 --mode llm --force`, then 7.
Book text is never rewritten by the LLM: it only returns line numbers and labels, and code copies
the text, then checks it.

## Deploy on a Hetzner VPS (CX23 or CAX11)

1. **DNS:** add an `A` record `ncert.hrishk.com` → the VPS IP.
2. **Server setup** (Ubuntu 24.04), as root:
   ```bash
   apt update && apt install -y docker.io docker-compose-v2 git sqlite3
   git clone <this repo> /opt/ncert-mcp && cd /opt/ncert-mcp
   cp .env.example .env && nano .env     # set SECRET_TOKEN
   mkdir -p state && chown 1000:1000 state
   docker compose up -d --build
   ```
   Caddy gets the HTTPS certificate by itself on first start.
3. **Check:** `curl https://ncert.hrishk.com/<SECRET_TOKEN>/health` shows `"status": "ok"` and the
   chapter counts. `curl https://ncert.hrishk.com/` returns 404, as it should.
4. **Firewall:** `sudo BLOCK_NET= deploy/harden_network.sh` opens 443 only to Anthropic's outbound
   range (the only client), keeps 80 open for certificate renewal, and hardens against SYN floods.
   After this, check health from the VPS itself: `curl http://127.0.0.1:8000/<SECRET_TOKEN>/health`.
5. **Backups:** `crontab -e` and add
   `15 2 * * * /opt/ncert-mcp/deploy/backup_state.sh >> /var/log/ncert-backup.log 2>&1`.
   For an off-box copy, put `BACKUP_REMOTE=user@host:path/` in `deploy/backup.env`.
6. **Connect claude.ai:** Settings → Connectors → Add custom connector →
   URL `https://ncert.hrishk.com/<SECRET_TOKEN>/mcp`. Then enable it in the Project.
7. **Update later:** `cd /opt/ncert-mcp && git pull && docker compose up -d --build`.
   Notes are in `./state/` on the VPS disk, so redeploys never touch them.

Admin (on the VPS): `docker compose exec app python -m admin.notes_cli history`.

## Moving hosts

All `state.db` access is in `server/state.py`. To move to another host, copy `state/state.db`
across. To switch to Turso or another database, rewrite only that one file.
