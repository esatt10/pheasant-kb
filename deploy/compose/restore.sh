#!/usr/bin/env bash
# Restore a pheasant-kb backup: `docker compose run --rm restore <name> [--force]`.
#
# <name> is a directory backup.sh wrote under ./backups. Safe by default, in
# the same way `pheasant restore` is for the single container:
#
#   * every file is checked against SHA256SUMS before anything is touched;
#   * it refuses while a pheasant service is running, because a restore under a
#     live indexer interleaves two histories;
#   * it refuses a stack that already holds data unless --force, and with
#     --force it replaces nothing it has not kept: the current database is
#     dumped to backups/pre-restore-<time>/ and the current /state and
#     /memory contents are moved aside into .replaced-<time>/ on their own
#     volumes. Delete those once the restored stack is confirmed.
#
# The database is restored in one transaction, so a failed restore leaves it
# as it was.
set -euo pipefail

BACKUPS="${BACKUPS_DIR:-/backups}"
STATE="${STATE_DIR:-/state}"
MEMORY="${MEMORY_DIR:-/memory}"
SECRETS="${SECRETS_DIR:-/secrets}"
WORKER_SECRET="${WORKER_SECRET_DIR:-/worker-secret}"
OWNER="${PHEASANT_OWNER:-10001:10001}"
SERVICES="${PHEASANT_SERVICES-api graph indexer logger}"

name=""
force=false
for arg in "$@"; do
    case "$arg" in
        --force) force=true ;;
        -*) echo "pheasant-kb: unknown option $arg" >&2; exit 2 ;;
        *) name="$arg" ;;
    esac
done
if [ -z "$name" ]; then
    echo "usage: docker compose run --rm restore <backup-name> [--force]" >&2
    echo "backups available:" >&2
    ls -1 "$BACKUPS" 2>/dev/null | grep -v '^\.' | grep -v '^pre-restore-' >&2 || echo "  (none)" >&2
    exit 2
fi
source_dir="$BACKUPS/$(basename "$name")"
if [ ! -f "$source_dir/SHA256SUMS" ] || [ ! -f "$source_dir/db.pgdump" ]; then
    echo "pheasant-kb: $source_dir is not a pheasant-kb backup (no SHA256SUMS or db.pgdump)" >&2
    exit 1
fi

newest_bin="$(ls -d "${PG_LIB_DIR:-/usr/lib/postgresql}"/*/bin 2>/dev/null | sort -V | tail -n 1 || true)"
if [ -n "$newest_bin" ]; then
    PATH="$newest_bin:$PATH"
fi

echo "pheasant-kb: verifying $name"
(cd "$source_dir" && sha256sum --check --quiet SHA256SUMS)

running=""
for service in $SERVICES; do
    if timeout 2 bash -c "exec 3<>/dev/tcp/$service/8765" 2>/dev/null; then
        running="$running $service"
    fi
done
if [ -n "$running" ]; then
    echo "pheasant-kb: refusing: still running:$running" >&2
    echo "pheasant-kb: stop them first: docker compose stop api graph indexer logger worker" >&2
    exit 1
fi

has_entries() {  # has_entries <dir>: anything besides earlier .replaced-* folders
    [ -d "$1" ] && [ -n "$(find "$1" -mindepth 1 -maxdepth 1 ! -name '.replaced-*' -print -quit)" ]
}
tables="$(psql --tuples-only --no-align --command \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
populated=""
[ "${tables:-0}" -gt 0 ] && populated="$populated database"
has_entries "$STATE" && populated="$populated /state"
has_entries "$MEMORY" && populated="$populated /memory"
if [ -n "$populated" ] && [ "$force" != true ]; then
    echo "pheasant-kb: refusing: this stack already holds data in:$populated" >&2
    echo "pheasant-kb: rerun with --force to replace it; the current data is kept aside, not deleted." >&2
    exit 1
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
if [ -n "$populated" ]; then
    keep="$BACKUPS/pre-restore-$stamp"
    mkdir -p "$keep"
    if [ "${tables:-0}" -gt 0 ]; then
        echo "pheasant-kb: keeping the current database in backups/pre-restore-$stamp"
        pg_dump --format=custom --no-owner --file="$keep/db.pgdump"
    fi
    if [ -d "$SECRETS" ] && [ -n "$(ls -A "$SECRETS")" ]; then
        (umask 077 && tar -czf "$keep/secrets.tar.gz" -C "$SECRETS" .)
    fi
    for dir in "$STATE" "$MEMORY"; do
        if has_entries "$dir"; then
            aside="$dir/.replaced-$stamp"
            echo "pheasant-kb: moving the current contents of $dir to $aside"
            mkdir "$aside"
            find "$dir" -mindepth 1 -maxdepth 1 ! -name '.replaced-*' -exec mv -t "$aside" -- {} +
        fi
    done
fi

echo "pheasant-kb: restoring the database"
pg_restore --clean --if-exists --no-owner --single-transaction --exit-on-error \
    --dbname="${PGDATABASE:-pheasant}" "$source_dir/db.pgdump"

unpack() {  # unpack <name> <dir>
    if [ -f "$source_dir/$1.tar.gz" ]; then
        echo "pheasant-kb: restoring $2"
        mkdir -p "$2"
        tar -xzf "$source_dir/$1.tar.gz" -C "$2"
    fi
}
unpack state "$STATE"
unpack memory "$MEMORY"
if [ -f "$source_dir/secrets.tar.gz" ]; then
    (umask 077 && unpack secrets "$SECRETS")
    if [ -f "$SECRETS/worker-token" ]; then
        mkdir -p "$WORKER_SECRET"
        cp "$SECRETS/worker-token" "$WORKER_SECRET/worker-token"
    fi
fi

if [ "$(id -u)" = 0 ]; then
    chown -R "$OWNER" "$STATE" "$MEMORY"
    for dir in "$SECRETS" "$WORKER_SECRET"; do
        [ -d "$dir" ] && chown -R "$OWNER" "$dir"
    done
fi
for dir in "$SECRETS" "$WORKER_SECRET"; do
    if [ -d "$dir" ] && [ -n "$(ls -A "$dir")" ]; then
        chmod 0700 "$dir"
        chmod 0400 "$dir"/*
    fi
done
if [ -n "${keep:-}" ]; then
    owner="$(stat -c %u:%g "$BACKUPS")"
    chown -R "$owner" "$keep" 2>/dev/null || true
fi

echo "pheasant-kb: restored $name. Start the stack: docker compose up -d"
echo "pheasant-kb: a token set in .env replaces the restored one on start."
