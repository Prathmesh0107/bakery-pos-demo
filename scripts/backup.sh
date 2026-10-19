#!/usr/bin/env bash
# Read-only backup of the live database to a verified, checksummed file. Never modifies the database.
#   DATABASE_URL=postgresql://... ./scripts/backup.sh          (optional: BACKUP_DIR=./backups)
set -euo pipefail
: "${DATABASE_URL:?Set DATABASE_URL to the database you want to back up}"
for t in pg_dump pg_restore psql sha256sum; do command -v "$t" >/dev/null || { echo "Missing tool: $t"; exit 1; }; done

# pg_dump must be at least as new as the server, otherwise the dump can fail or be incomplete.
srv=$(psql "$DATABASE_URL" -At -c "SHOW server_version_num"); srv_major=$((srv / 10000))
cli_major=$(pg_dump --version | grep -oE '[0-9]+' | head -1)
[ "$cli_major" -ge "$srv_major" ] || { echo "pg_dump v$cli_major is older than the server (v$srv_major). Install a newer postgresql-client."; exit 1; }

dir="${BACKUP_DIR:-./backups}"; umask 077; mkdir -p "$dir"
f="$dir/rushpos_$(date -u +%Y%m%dT%H%M%SZ).dump"
counts() { psql "$DATABASE_URL" -At -c "SELECT 'products',count(*) FROM products UNION ALL SELECT 'sales',count(*) FROM sales UNION ALL SELECT 'stock_log',count(*) FROM stock_log"; }

counts > "$f.counts.before"
pg_dump --format=custom --no-owner --no-privileges --file="$f.partial" "$DATABASE_URL"
counts > "$f.counts.after"
pg_restore --list "$f.partial" > /dev/null          # the archive must be readable before we call it a backup
mv "$f.partial" "$f"; sha256sum "$f" > "$f.sha256"

echo "Backup OK: $f ($(du -h "$f" | cut -f1))"; echo "Rows at backup time (before/after):"; paste "$f.counts.before" "$f.counts.after"
echo "NEXT: copy it OFF this machine, and run scripts/restore_verify.sh on it at least once before you rely on it."
