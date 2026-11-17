#!/usr/bin/env bash
# pheasant's PostgreSQL entrypoint: the official one, behind one refusal.
#
# This image keeps its data in a new volume. A stack upgrading from the
# TimescaleDB image still has its database in the previous one, and the
# official entrypoint, finding its own data directory empty, would initialize
# a new, empty cluster -- the stack would come up with nothing in it and say
# nothing. So when the previous data directory is mounted (compose.yaml mounts
# the old volume read-only at $PHEASANT_PREVIOUS_PGDATA) and holds a cluster,
# while this one is empty, it refuses and says how to move the data:
# deploy/postgres/README.md#upgrading-an-existing-stack.
#
# Set PHEASANT_POSTGRES_ALLOW_FRESH=1 to initialize anyway -- the step of that
# procedure that creates the new cluster a backup is then restored into.
set -euo pipefail

previous="${PHEASANT_PREVIOUS_PGDATA:-/var/lib/pheasant-previous-postgres}"
current="${PGDATA:-/var/lib/postgresql/data}"

if [ "${1:-postgres}" = postgres ] \
    && [ ! -s "$current/PG_VERSION" ] \
    && { [ -s "$previous/PG_VERSION" ] || [ -s "$previous/data/PG_VERSION" ]; } \
    && [ "${PHEASANT_POSTGRES_ALLOW_FRESH:-}" != 1 ]; then
    cat >&2 <<EOF
pheasant-postgres: refusing to initialize an empty database.

The previous database volume (mounted at $previous) holds a PostgreSQL
cluster, and this image's data directory ($current) is empty. Starting
would give the stack an empty database while the old one sits unused.

Move the data with a backup and restore (nothing is deleted):
  1. with the stack still on its previous image: docker compose run --rm backup
  2. upgrade, then:  PHEASANT_POSTGRES_ALLOW_FRESH=1 docker compose up -d postgres
  3.                 docker compose run --rm restore <backup-name>
  4.                 docker compose up -d
See deploy/postgres/README.md#upgrading-an-existing-stack.
EOF
    exit 64
fi

exec docker-entrypoint.sh "$@"
