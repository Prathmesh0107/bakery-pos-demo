#!/usr/bin/env bash
# Proves a backup is restorable: restores it into an EMPTY scratch database and checks the data.
# It can never touch the live database: it refuses if the target equals DATABASE_URL or is not empty.
#   RESTORE_DATABASE_URL=postgresql://.../scratch CONFIRM_SCRATCH_RESTORE=yes ./scripts/restore_verify.sh backups/rushpos_X.dump
set -euo pipefail
f="${1:?usage: restore_verify.sh path/to/rushpos_X.dump}"
: "${RESTORE_DATABASE_URL:?Set RESTORE_DATABASE_URL to an EMPTY scratch database (NOT the live one)}"
[ "${CONFIRM_SCRATCH_RESTORE:-}" = "yes" ] || { echo "Set CONFIRM_SCRATCH_RESTORE=yes to confirm the target is a scratch database."; exit 1; }
for t in pg_restore psql sha256sum; do command -v "$t" >/dev/null || { echo "Missing tool: $t"; exit 1; }; done
[ -f "$f" ] || { echo "No such file: $f"; exit 1; }
if [ -n "${DATABASE_URL:-}" ] && [ "${RESTORE_DATABASE_URL%%\?*}" = "${DATABASE_URL%%\?*}" ]; then
  echo "REFUSED: RESTORE_DATABASE_URL is the same as DATABASE_URL (the live database)."; exit 1; fi

[ -f "$f.sha256" ] && sha256sum -c "$f.sha256" || { echo "Checksum file missing or file is corrupt."; exit 1; }
tables=$(psql "$RESTORE_DATABASE_URL" -At -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
[ "$tables" = "0" ] || { echo "REFUSED: target database is not empty ($tables tables). Create a fresh empty database."; exit 1; }

pg_restore --no-owner --no-privileges --exit-on-error --single-transaction -d "$RESTORE_DATABASE_URL" "$f"
q() { psql "$RESTORE_DATABASE_URL" -At -c "$1"; }
fail=0
# Tables only ever grow (nothing deletes sales/products/log), so restored counts must sit between the before/after counts.
while IFS='|' read -r t b; do
  a=$(grep "^$t|" "$f.counts.after" | cut -d'|' -f2); r=$(q "SELECT count(*) FROM $t")
  if [ "$r" -ge "$b" ] && [ "$r" -le "$a" ]; then echo "OK    $t: restored $r (backup window $b..$a)"; else echo "FAIL  $t: restored $r, expected $b..$a"; fail=1; fi
done < "$f.counts.before"
[ "$(q "SELECT count(*) FROM products WHERE stock < 0")" = "0" ] && echo "OK    no negative stock" || { echo "FAIL  negative stock found"; fail=1; }
[ "$(q "SELECT count(*) FROM (SELECT bill_no FROM sales GROUP BY 1 HAVING count(*)>1) d")" = "0" ] && echo "OK    bill numbers unique" || { echo "FAIL  duplicate bill numbers"; fail=1; }
drift=$(q "SELECT count(*) FROM products p WHERE EXISTS (SELECT 1 FROM stock_log l WHERE l.product_id=p.id) AND p.stock <> (SELECT l.after_qty FROM stock_log l WHERE l.product_id=p.id ORDER BY l.id DESC LIMIT 1)")
[ "$drift" = "0" ] && echo "OK    stock matches the stock log for every logged product" || echo "WARN  $drift product(s) where stock differs from the latest stock-log entry (investigate; may predate the stale-stock fix)"
[ "$fail" = "0" ] && echo "RESTORE VERIFIED. Drop the scratch database when done." || { echo "RESTORE CHECKS FAILED - do not trust this backup."; exit 1; }
