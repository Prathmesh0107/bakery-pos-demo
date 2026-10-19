# Backup and restore

Needs `postgresql-client` (`pg_dump`, `pg_restore`, `psql`) at least as new as the server. Scripts are read-only on the live database.

## Take a backup (before every deploy, and daily during the trial)
    export DATABASE_URL='postgresql://...'      # the LIVE database
    ./scripts/backup.sh                          # -> backups/rushpos_<UTC time>.dump (+ .sha256, row counts)
Copy the file **off the machine** (and off Render). Keep several; encrypt if stored in shared storage (`gpg -c file`).

## Prove it restores (do this once now, then monthly)
    createdb rushpos_scratch                     # or create an empty DB in your provider
    export RESTORE_DATABASE_URL='postgresql://.../rushpos_scratch'
    CONFIRM_SCRATCH_RESTORE=yes ./scripts/restore_verify.sh backups/rushpos_X.dump
It checks the checksum, refuses a non-empty target or the live URL, restores in one transaction, then compares row counts, negative stock, duplicate bill numbers and stock-vs-log. A backup is not a backup until this passes. Then drop the scratch DB.

## Disaster recovery (live database lost or damaged)
1. Do **not** restore over the live database. Create a **new empty** database.
2. Run `restore_verify.sh` into it with the newest good backup; read every line.
3. In the web service environment, change `DATABASE_URL` to the new database and redeploy. Check a few bills and stock levels.
4. Rollback = set `DATABASE_URL` back. Data entered after the backup time is lost: reconcile against the paper/printed receipts.
5. Record what happened.

## Deploy rule
Never deploy a schema/code change to the real outlet unless a backup from the last hour has passed `restore_verify.sh`.
