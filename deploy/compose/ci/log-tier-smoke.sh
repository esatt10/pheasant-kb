#!/usr/bin/env bash
# Prove the log tier works across a container boundary.
#
# "A row exists" is not the assertion -- an in-process test already shows that.
# What has to be proved is that a *separate process* does the draining, because
# if the producer ever started draining its own batches every other check here
# would still pass while the tier did nothing.
#
# So the run is split in two phases:
#
#   1. api only, no logger. Batches must pile up on the log tier's JetStream
#      stream and `interaction_events` must stay **empty** -- the producer does
#      not drain.
#   2. start the logger. The rows must appear and the stream must empty: the
#      stream uses work-queue retention, so an acked batch leaves it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE=(docker compose -f "${HERE}/docker-compose.log-tier.yml")
# Matches PHEASANT_API_TOKEN in that file. The api replica binds 0.0.0.0, so
# it refuses to start unauthenticated -- and every call here carries the token,
# which means this script also proves the guard admits a real caller.
API_TOKEN="ci-smoke-token"
PSQL=("${COMPOSE[@]}" exec -T postgres psql -qtAX -U pheasant -d pheasant -c)
# NATS monitoring, published on the host by the compose file.
NATS_MONITOR="http://127.0.0.1:18222"
LOG_STREAM="PHEASANT_LOGS"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

query() { "${PSQL[@]}" "$1" | tr -d '[:space:]'; }

# Messages currently held on a stream (0 when it does not exist yet).
stream_messages() {
  curl -fsS "${NATS_MONITOR}/jsz?streams=true" | python3 -c '
import json, sys
name = sys.argv[1]
data = json.load(sys.stdin)
for account in data.get("account_details") or []:
    for stream in account.get("stream_detail") or []:
        if stream.get("name") == name:
            print(stream.get("state", {}).get("messages", 0))
            raise SystemExit(0)
print(0)
' "$1"
}

await() {
  # $1 label, $2 command producing a number, $3 seconds; waits for non-zero
  local label="$1" command="$2" seconds="$3" waited=0 value
  while [ "${waited}" -lt "${seconds}" ]; do
    value="$(eval "${command}" || true)"
    if [ -n "${value}" ] && [ "${value}" != "0" ]; then
      echo "    ${label}: ${value}"
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  echo "TIMED OUT after ${seconds}s waiting for ${label} (last value: '${value:-}')" >&2
  return 1
}

log "Phase 1: postgres, nats, the migrator and an api replica -- deliberately no logger"
"${COMPOSE[@]}" up -d --wait --wait-timeout 240 postgres nats db-init api

# The corpus was indexed by the migrator: an `api` replica publishes index
# work rather than running it, and this topology has no indexer on purpose.
log "Driving real searches through the api replica"
for query_text in "pheasant-flock rollout" "filewatch daemon nightly" "vault seal rotation"; do
  curl -fsS -X POST localhost:8765/search \
    -H "authorization: Bearer ${API_TOKEN}" \
    -H 'content-type: application/json' \
    -H 'X-Pheasant-Session: ci-session' \
    -H 'X-Pheasant-Principal: user:ci' \
    -H "traceparent: 00-$(printf 'a%.0s' {1..32})-$(printf 'b%.0s' {1..16})-01" \
    -d "{\"query\": \"${query_text}\", \"mode\": \"hybrid\", \"max_results\": 5}" >/dev/null
done

log "The api published batches to the log tier's own stream"
await "batches on ${LOG_STREAM}" "stream_messages ${LOG_STREAM}" 60

log "...and did NOT drain them itself"
# The half that can fail silently. Give it long enough that a producer which
# *did* drain would have finished: the flush interval is 1s and there is no
# scheduler beat at all in this config.
sleep 10
DRAINED_WITHOUT_LOGGER="$(query 'SELECT count(*) FROM interaction_events;')"
PENDING="$(stream_messages "${LOG_STREAM}")"
echo "    interaction_events without a logger: ${DRAINED_WITHOUT_LOGGER}"
echo "    batches still on the stream:         ${PENDING}"
if [ "${DRAINED_WITHOUT_LOGGER}" != "0" ]; then
  echo "FAILED: the api drained its own batches; the log tier would be doing nothing" >&2
  exit 1
fi
test "${PENDING}" != "0"

log "Phase 2: starting the logger"
"${COMPOSE[@]}" up -d --wait --wait-timeout 180 logger

log "The logger drained them into the hot store"
await "interaction_events rows" "query 'SELECT count(*) FROM interaction_events;'" 90

log "The rows carry identity, modality and the caller's own trace"
test "$(query "SELECT count(*) FROM interaction_events WHERE session_id='ci-session';")" != "0"
test "$(query "SELECT count(*) FROM interaction_events WHERE principal='user:ci';")" != "0"
test "$(query "SELECT count(*) FROM interaction_events WHERE modality='ui';")" != "0"
# Timestamps and traces are guaranteed, not best-effort.
test "$(query "SELECT count(*) FROM interaction_events WHERE trace_id IS NULL OR started_at IS NULL OR duration_ms IS NULL;")" = "0"
# The agent's inbound trace was adopted rather than replaced.
test "$(query "SELECT count(*) FROM interaction_events WHERE trace_id = repeat('a', 32);")" != "0"
echo "    identity, modality, trace and timing all present"

log "Every batch was acknowledged: the stream is empty"
waited=0
while [ "$(stream_messages "${LOG_STREAM}")" != "0" ] && [ "${waited}" -lt 60 ]; do
  sleep 2
  waited=$((waited + 2))
done
test "$(stream_messages "${LOG_STREAM}")" = "0"

log "The log tier is real"
