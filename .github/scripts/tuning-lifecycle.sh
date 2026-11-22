#!/usr/bin/env bash
# Walk the tuning surface an operator actually touches, against a real region.
#
# Every in-process test calls Python functions directly. None of them would
# notice a mis-wired argparse subcommand, a printer reading a payload shape the
# report does not have, or a command that exits non-zero for correctly
# declining to change anything. All three are the kind of thing that reaches a
# user before it reaches us, and two of them already have.
#
# What this asserts, in order:
#
#   1. `tune diagnose` names the stages, and labels which are reachable.
#   2. `tune run` completes and never exits non-zero for a *result* — a batch
#      that proposes nothing, or whose winner fails a gate, has done its job.
#   3. `tune show` reports `config` before anything is applied.
#   4. `tune apply` makes a bundle live and `tune show` says `bundle`.
#   5. `tune rollback` returns the region to its configured values.
#   6. Applying an unknown bundle is a refusal with a non-zero exit, not a
#      silent success that leaves ranking unchanged.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
LOG="${ROOT}/tuning-lifecycle.log"
: >"${LOG}"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*" | tee -a "${LOG}"; }
run() { "$@" 2>&1 | tee -a "${LOG}"; }

cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT

log "Building a region with real queries, real proof, and a real deficiency"
# The same seeding the render check uses. Shared rather than duplicated, and
# deliberately not an import from `tests/`: a CI script coupled to test
# internals breaks whenever a fixture is renamed, for a check that has nothing
# to do with the tests.
run python scripts/seed_render_region.py --work "${WORK}"

CONFIG="${WORK}/pheasant.yaml"

log "1. Diagnose: which step is losing documents"
run python -m pheasant tune diagnose -c "${CONFIG}"

# The diagnosis has to name stages and say whether each is reachable. A
# histogram without that distinction invites the same response to "the fusion
# demoted it" and "it was never indexed".
python - "${LOG}" <<'PY'
import sys

body = open(sys.argv[1], encoding="utf-8").read()
assert "evidenced query/target pairs" in body, "the diagnosis reported no denominator"
assert "tunable" in body or "no misses to attribute" in body, (
    "the diagnosis did not say whether any stage is reachable by tuning"
)
print("diagnosis names its stages and its denominator")
PY

log "2. Run a batch"
run python -m pheasant tune run -c "${CONFIG}"

log "3. Before applying anything, the region is on its configured values"
run python -m pheasant tune show -c "${CONFIG}"
python -m pheasant tune show -c "${CONFIG}" | grep -q "parameters from: config" \
  || { echo "expected the region to be on config parameters"; exit 1; }

log "4. Produce a bundle and apply it"
BUNDLE="$(python - "${CONFIG}" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, ".")
from pheasant.config.loader import load_config  # noqa: E402
from pheasant.persistence.state_store import StateStore  # noqa: E402
from pheasant.tuning import store as tuning_store  # noqa: E402
from pheasant.tuning.contracts import TuningBundle  # noqa: E402

config = load_config(Path(sys.argv[1]))
state = StateStore.from_config(config)
state.migrate()
existing = tuning_store.list_bundles(state, config.knowledge_base_id)
if existing:
    print(existing[0]["bundle_id"])
else:
    # The batch is allowed to conclude that nothing beats the current
    # configuration — that is a result, and it must not make this job red. The
    # apply/rollback path still has to work, so a bundle is created directly.
    parameters = {"title_weight": 12.0}
    bundle = TuningBundle(
        bundle_id=TuningBundle.identity(parameters),
        kb_id=config.knowledge_base_id,
        experiment_id="exp-lifecycle",
        decision_id="dec-lifecycle",
        snapshot_id="snap-lifecycle",
        parameters=parameters,
        rationale="lifecycle check",
    )
    tuning_store.save_bundle(state, bundle)
    print(bundle.bundle_id)
state.close()
PY
)"
echo "bundle: ${BUNDLE}" | tee -a "${LOG}"

run python -m pheasant tune apply "${BUNDLE}" -c "${CONFIG}"
python -m pheasant tune show -c "${CONFIG}" | grep -q "parameters from: bundle" \
  || { echo "the applied bundle did not become the region's overlay"; exit 1; }
echo "    the overlay is live" | tee -a "${LOG}"

log "5. Roll back"
run python -m pheasant tune rollback -c "${CONFIG}"
python -m pheasant tune show -c "${CONFIG}" | grep -q "parameters from: config" \
  || { echo "rollback did not return the region to its configured values"; exit 1; }
echo "    back on configured values" | tee -a "${LOG}"

log "6. An unknown bundle is a refusal, not a silent no-op"
if python -m pheasant tune apply bundle-does-not-exist -c "${CONFIG}" >>"${LOG}" 2>&1; then
  echo "applying an unknown bundle succeeded; it must refuse" | tee -a "${LOG}"
  exit 1
fi
echo "    refused, with a non-zero exit" | tee -a "${LOG}"

log "7. Status and report are readable after the fact"
run python -m pheasant tune status -c "${CONFIG}"
run python -m pheasant tune bundles -c "${CONFIG}"
python -m pheasant tune report -c "${CONFIG}" --json >/dev/null \
  || { echo "the stored report is not readable"; exit 1; }
echo "    status, bundles and report all read back" | tee -a "${LOG}"

log "Lifecycle complete"
