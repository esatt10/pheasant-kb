#!/bin/sh
# First-boot secrets for the pheasant-kb stack (compose.yaml, `secrets-init`).
#
# The stack has four boundaries and needs four distinct tokens (see the
# comment above `x-kb-env` in compose.yaml). Asking a person to generate them
# by hand is the step that kept the fleet from being a one-line start, so this
# writes them into volumes once and every tier reads its own as a file through
# the image's `*_FILE` convention.
#
# For each token, in order:
#   1. a value set in the environment (`.env`) wins and is written, so an
#      operator can still choose, rotate or pin one;
#   2. otherwise a token already on the volume is kept -- a restart must never
#      rotate a credential agents and browsers are already holding;
#   3. otherwise a new 32-byte random token is generated.
#
# The worker token is also copied to a second volume by itself, because the
# worker tier is the least trusted and must hold nothing else; mounting the
# shared secrets volume there would hand every worker the API and graph
# credentials, which `pheasant worker` refuses to start with.
set -eu

SECRETS_DIR="${SECRETS_DIR:-/secrets}"
WORKER_SECRET_DIR="${WORKER_SECRET_DIR:-/worker-secret}"
OWNER="${SECRETS_OWNER:-10001:10001}"

umask 077
mkdir -p "$SECRETS_DIR" "$WORKER_SECRET_DIR"

random_token() {
    if command -v python >/dev/null 2>&1; then
        python -c 'import secrets; print(secrets.token_hex(32))'
    else
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
    fi
}

write_secret() {
    printf '%s\n' "$2" > "$1.tmp"
    mv "$1.tmp" "$1"
}

for pair in \
    api-token:PHEASANT_API_TOKEN \
    graph-token:PHEASANT_GRAPH_SERVICE_TOKEN \
    worker-token:PHEASANT_INDEX_WORKER_TOKEN \
    ingestion-token:PHEASANT_INGESTION_SERVICE_TOKEN
do
    file="$SECRETS_DIR/${pair%%:*}"
    name="${pair#*:}"
    eval "given=\${$name:-}"
    if [ -n "$given" ]; then
        write_secret "$file" "$given"
        echo "pheasant-kb: $name taken from the environment."
    elif [ -s "$file" ]; then
        echo "pheasant-kb: $name kept from an earlier boot."
    else
        write_secret "$file" "$(random_token)"
        echo "pheasant-kb: $name generated."
    fi
done

write_secret "$WORKER_SECRET_DIR/worker-token" "$(cat "$SECRETS_DIR/worker-token")"

# The tiers run as the image's unprivileged user; this container runs as root
# only because Docker creates named-volume roots root-owned.
if [ "$(id -u)" = 0 ]; then
    chown -R "$OWNER" "$SECRETS_DIR" "$WORKER_SECRET_DIR"
fi
chmod 0700 "$SECRETS_DIR" "$WORKER_SECRET_DIR"
chmod 0400 "$SECRETS_DIR"/* "$WORKER_SECRET_DIR"/*
