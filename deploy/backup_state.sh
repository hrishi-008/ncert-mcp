#!/usr/bin/env bash
# Daily backup of the teacher's notes + paper log (state.db).
# Install on the VPS:  crontab -e  ->  15 2 * * * /opt/ncert-mcp/deploy/backup_state.sh
# Uses SQLite's online backup, so it is safe while the server is running.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
stamp=$(date +%Y-%m-%d)
python3 - "$stamp" <<'PY'
import sqlite3, sys
src = sqlite3.connect("state/state.db")
dst = sqlite3.connect(f"backups/state-{sys.argv[1]}.db")
src.backup(dst)
dst.close(); src.close()
PY
gzip -f "backups/state-$stamp.db"
# Keep 60 days locally.
find backups -name 'state-*.db.gz' -mtime +60 -delete
# Optional off-box copy: set BACKUP_REMOTE (e.g. "user@host:ncert-backups/") in deploy/backup.env
if [ -f deploy/backup.env ]; then
  # shellcheck disable=SC1091
  . deploy/backup.env
  if [ -n "${BACKUP_REMOTE:-}" ]; then
    scp -q "backups/state-$stamp.db.gz" "$BACKUP_REMOTE"
  fi
fi
echo "backed up state-$stamp.db.gz"
