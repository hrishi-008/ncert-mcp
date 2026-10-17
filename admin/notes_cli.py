"""Admin tool for the teacher notes (HK only; NOT exposed as an MCP tool).

    python -m admin.notes_cli list                 # current notes
    python -m admin.notes_cli list --all           # including deleted ones
    python -m admin.notes_cli history [NOTE_ID]    # every change, with reasons
    python -m admin.notes_cli restore HISTORY_ID   # put a note back to how it was at that change
    python -m admin.notes_cli papers               # recent logged papers

On the VPS, run it inside the container so it uses the same state.db:
    docker compose exec app python -m admin.notes_cli history
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from server.state import State

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("STATE_DB_URL", f"sqlite:///{ROOT / 'data' / 'state.db'}"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("history")
    p.add_argument("note_id", nargs="?", type=int)
    p = sub.add_parser("restore")
    p.add_argument("history_id", type=int)
    p.add_argument("--reason", default="restored by admin")
    sub.add_parser("papers")
    args = ap.parse_args()
    st = State(args.db)

    if args.cmd == "list":
        for n in st.notes(include_inactive=args.all):
            flag = "" if n["active"] else "  (deleted)"
            print(f"[{n['note_id']}] {n['section']}: {n['text']}{flag}")
    elif args.cmd == "history":
        for h in st.history(args.note_id):
            print(f"#{h['id']} {h['at']} note {h['note_id']} {h['action'].upper()}  reason: {h['reason']}")
            if h["old_text"]:
                print(f"     before: {h['old_text']}")
            if h["new_text"]:
                print(f"     after:  {h['new_text']}")
    elif args.cmd == "restore":
        res = st.restore(args.history_id, args.reason)
        print(f"Restored note {res['note_id']}: {res['text']}")
    elif args.cmd == "papers":
        for p in st.papers():
            print(f"paper {p['paper_id']} {p['created_at'][:10]} class {p['class']} {p['subject']} "
                  f"{p['chapter_ids_json']} {p['total_marks']:g} marks")


if __name__ == "__main__":
    main()
