#!/usr/bin/env bash
# Back up a pheasant-kb stack: `docker compose run --rm backup`.
#
# Runs in the Postgres image (compose.yaml, profile `tools`), so pg_dump is
# always the server's own major version. One backup is one directory under
# ./backups (PHEASANT_BACKUP_PATH), holding everything a region needs to come
# back exactly as it was:
#
#   db.pgdump          the database: state, chunks, graph rows, BM25 index
#   state.tar.gz       /state: vectors, graph snapshots, media, uploads
#   memory.tar.gz      /memory: agent memory records
#   secrets.tar.gz     the four tokens, so agents and browsers keep working
#   SHA256SUMS         what restore.sh verifies before it touches anything
#
# The database and /state have to be copied together and from the same
# moment, which is why this is one command and not two. pg_dump is a
# consistent snapshot on its own; /state is not, so for an exact pair stop the
# indexer first (`docker compose stop indexer`). An online backup is still
# usable: run a repair sync after restoring it, which reconciles vectors with
# the chunks the database holds.
#
# The directory is written as `.<name>.partial` and renamed when complete, so
# a backup that failed half way never looks like one that finished.
set -euo pipefail

BACKUPS="${BACKUPS_DIR:-/backups}"
STATE="${STATE_DIR:-/state}"
MEMORY="${MEMORY_DIR:-/memory}"
SECRETS="${SECRETS_DIR:-/secrets}"
# Services that write what is being copied; reachable means running.
WRITERS="${PHEASANT_WRITERS-indexer}"

# timescaledb-ha carries several Postgres majors; use the newest client,
# which is never older than the server.
newest_bin="$(ls -d "${PG_LIB_DIR:-/usr/lib/postgresql}"/*/bin 2>/dev/null | sort -V | tail -n 1 || true)"
if [ -n "$newest_bin" ]; then
    PATH="$newest_bin:$PATH"
fi

running() {
    timeout 2 bash -c "exec 3<>/dev/tcp/$1/8765" 2>/dev/null
}

for writer in $WRITERS; do
    if running "$writer"; then
        echo "pheasant-kb: WARNING: $writer is running, so /state may move during the copy." >&2
        echo "pheasant-kb: for an exact backup: docker compose stop $writer, back up, docker compose start $writer." >&2
        echo "pheasant-kb: continuing; run a repair sync after restoring this backup." >&2
    fi
done

name="pheasant-kb-$(date -u +%Y%m%dT%H%M%SZ)"
work="$BACKUPS/.$name.partial"
mkdir -p "$BACKUPS"
rm -rf "$work"
mkdir "$work"
trap 'rm -rf "$work"' EXIT

echo "pheasant-kb: dumping the database"
pg_dump --format=custom --no-owner --file="$work/db.pgdump"

archive() {  # archive <name> <dir>
    if [ -d "$2" ]; then
        echo "pheasant-kb: archiving $2"
        tar -czf "$work/$1.tar.gz" -C "$2" --exclude='./.replaced-*' .
    fi
}
archive state "$STATE"
archive memory "$MEMORY"
(umask 077 && archive secrets "$SECRETS")

{
    echo "created_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "pg_dump=$(pg_dump --version)"
} > "$work/backup.txt"
(cd "$work" && sha256sum -- *.pgdump *.tar.gz backup.txt > SHA256SUMS)

mv "$work" "$BACKUPS/$name"
trap - EXIT

# Hand the files to whoever owns the backups directory on the host, rather
# than leaving root-owned files in a user's folder.
owner="$(stat -c %u:%g "$BACKUPS")"
chown -R "$owner" "$BACKUPS/$name" 2>/dev/null || true

echo "pheasant-kb: backup written: $name (in PHEASANT_BACKUP_PATH, ./backups by default)"
echo "pheasant-kb: restore it with: docker compose run --rm restore $name"
