"""The whole loop, once, against real machinery.

Every other memory test checks one hop. This one commits the full exchange a
region actually performs, in order, with nothing stubbed between the steps:

    a search  ->  an OTel span + a ledger event
              ->  a batch published to the log tier's own queue
              ->  drained by a worker into the hot store
              ->  its recorded keys resolving back to the indexed text
              ->  rolled to cold Parquet under /exports
              ->  read back out of cold storage by DuckDB
              ->  mined into a memory candidate
              ->  promoted through MemoryStore.append
              ->  indexed by the ordinary pipeline
              ->  found again by search

The point is the seams. Each hop has its own unit tests; what those cannot
show is that the ids line up across all of them --- that the span the operator
sees, the row formation counts, the Parquet an outside reader gets and the
record a search returns are all the *same* interaction. A break anywhere in
that chain is invisible to a test of either side of it.

Written to be measurable as well as correct: the phase timings it prints are
what CI reports, so a regression in the loop shows up as a number rather than
as a feeling.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from pheasant.artifacts.formation import admit, run_candidate_rules, run_session_digests
from pheasant.artifacts.store import MemoryStore, memory_source
from pheasant.config.schema import PheasantConfig
from pheasant.sync import log_queue as lq
from pheasant.sync.queue import drain

pytestmark = pytest.mark.module_database

#: Phase timings, printed at the end so CI has a number to report.
TIMINGS: dict[str, float] = {}


class _Phase:
    def __init__(self, name: str) -> None:
        self.name = name

    def __enter__(self) -> _Phase:
        self._started = time.perf_counter()
        return self

    def __exit__(self, *_exc: Any) -> None:
        TIMINGS[self.name] = round((time.perf_counter() - self._started) * 1000.0, 1)


@pytest.fixture(scope="module")
def region(tmp_path_factory: Any) -> Any:
    """A region with every axis of the observation plane switched on.

    Deliberately the *scaled* shape rather than the default: the log queue is
    enabled, so batches take the same path they would on a `--role logger`
    tier, and cold storage is on, so the roll writes real Parquet. A loop
    tested only in the single-container shape would not exercise the hand-off
    the fleet actually depends on.
    """

    root = tmp_path_factory.mktemp("roundtrip")
    docs = root / "ws" / "docs" / "deploy"
    docs.mkdir(parents=True)
    (docs / "rollout.md").write_text(
        "# Rollout\n\nThe pheasant-flock service coordinates every rollout and canary.\n",
        encoding="utf-8",
    )
    (docs / "canary.md").write_text(
        "# Canary\n\nCanary steps are driven by the pheasant-flock service before promotion.\n",
        encoding="utf-8",
    )
    for name in ("state", "exports", "memory"):
        (root / name).mkdir(parents=True, exist_ok=True)

    raw = {
        "pheasant": {
            "name": "roundtrip",
            "state_path": str(root / "state"),
            "workspace_root": str(root / "ws"),
            "exports_path": str(root / "exports"),
        },
        "storage": {"graph_snapshots": False},
        "observability": {
            "interactions": {
                "enabled": True,
                "flush_batch_size": 1,
                "hot_retention_days": 0,
                "cold_enabled": True,
                "queue": {"enabled": True, "backend": "local"},
            }
        },
        "memory": {"formation": {"enabled": True, "min_observations": 2, "min_sessions": 2}},
        "sources": [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(docs),
                "include": ["**/*.md"],
                "sync": {"on_startup": False},
            },
            {
                "name": "agent-memory",
                "type": "memory",
                "path": str(root / "memory"),
                "sync": {"on_startup": False},
            },
        ],
    }
    config_path = root / "pheasant.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    from pheasant.api.app import create_app

    app = create_app(PheasantConfig.model_validate(raw), config_path=str(config_path))
    app.state.engine.sync_source("docs", "full")
    return {"app": app, "root": root, "config": app.state.config}


def test_the_whole_loop_from_a_search_to_a_memory_a_search_finds(region: Any) -> None:
    from fastapi.testclient import TestClient

    from pheasant.telemetry.interactions import TRACING

    app = region["app"]
    engine = app.state.engine
    state = engine.state
    root: Path = region["root"]

    # -- 1. real searches, through the real HTTP surface --------------------
    # Non-zero on purpose: the spec reserves all-zero ids as invalid, and
    # `parse_traceparent` rejects them -- so a fixture built from `{0:032x}`
    # would silently test the "start a fresh trace" path instead of the
    # propagation one it means to.
    traces = {"sess-a": "a" * 32, "sess-b": "b" * 32}
    parents = {"sess-a": "1" * 16, "sess-b": "2" * 16}

    # One client for the whole loop. The SDK's streamable-HTTP session manager
    # can only run once per app, so a second `TestClient(app)` would fail --
    # and holding one open is the more honest shape anyway: this is what a
    # live server doing all of it looks like.
    with TestClient(app) as client:
        with _Phase("search"):
            for session in ("sess-a", "sess-b"):
                # The third genuinely returns nothing on this corpus. Phrasing
                # matters more than it looks: "how do I rotate the vault seal"
                # comes back with every document, because the framing words match
                # even though none of the content words do. That is precisely why
                # `retrieval-gap-v1` keys off an empty result set rather than a
                # score floor -- a threshold would have to be tuned per corpus and
                # per mode to tell those two questions apart.
                for query in ("router rollout", "router canary", "vault seal rotation"):
                    response = client.post(
                        "/search",
                        json={"query": query, "mode": "hybrid", "max_results": 5},
                        headers={
                            "X-Pheasant-Session": session,
                            "X-Pheasant-Principal": "user:ada",
                            # An agent's own trace. It must survive the whole way.
                            "traceparent": f"00-{traces[session]}-{parents[session]}-01",
                        },
                    )
                    assert response.status_code == 200
            buffer = app.state.interaction_buffer
            assert buffer is not None, "observation is enabled; there must be a buffer"
            # The sink is the queue, not a direct write: this is the fleet path.
            assert buffer.sink_name == "queue"
            buffer.flush()

        # -- 2. the batch is on the log tier's own queue ------------------------
        log_q = app.state.log_queue
        assert log_q is not None
        depth = log_q.depth()
        assert depth["pending"] >= 1, "nothing reached the log queue"
        # Its *own* table. An indexer draining index_tasks must never see these.
        assert state.rows("SELECT COUNT(*) AS c FROM index_tasks", ())[0]["c"] == 0

        # -- 3. drained by a worker, exactly as `--role logger` would ------------
        with _Phase("drain"):
            written = drain(log_q, lambda task: lq.handle_batch(state, task), owner="logger-1")
        assert sum(written) >= 6
        hot = lq.hot_row_count(state)
        assert hot >= 6

        # -- 4. the ids line up: agent -> ledger -> (span, when exported) -------
        rows = [
            dict(row)
            for row in state.rows(
                "SELECT trace_id, span_id, parent_span_id, session_id, principal, modality, "
                "query_text, result_ids_json, result_paths_json, result_count, duration_ms, "
                "started_at, status FROM interaction_events WHERE operation='/search' "
                "ORDER BY started_at",
                (),
            )
        ]
        assert len(rows) >= 6
        for row in rows:
            # Timestamps and traces are guaranteed, not best-effort.
            assert row["trace_id"] and row["span_id"] and row["started_at"]
            assert row["duration_ms"] is not None
            assert row["principal"] == "user:ada"
            assert row["modality"] == "ui"
        # The caller's trace was adopted rather than replaced.
        assert {row["trace_id"] for row in rows} == set(traces.values())
        assert all(row["parent_span_id"] for row in rows)
        # With no exporter configured nothing leaves the box, which is what keeps
        # this test offline -- the ids are ours and they are still correlated.
        assert TRACING.enabled is False

        answered = [row for row in rows if row["query_text"] == "router rollout"]
        assert answered and json.loads(answered[0]["result_paths_json"] or "[]"), (
            "the corpus should answer this one"
        )

        # -- 5. a recorded key resolves back to the text it names ---------------
        # The join the review UI's side panel makes, and the one hop in this
        # chain with no test of its own: `result_ids` are only evidence if they
        # still address something. They are written by `extract_results` and
        # read by `/nodes/content`, two modules that share no code and could
        # drift apart on the id grammar without either side going red --- which
        # would leave a reviewer a list of keys that resolve to nothing.
        #
        # `_chunk_texts` is why these are artifact ids rather than chunk ids:
        # the alias rule asks whether the *document* that matched contains a
        # token, so narrowing them to the one matching chunk would answer a
        # different question. Asserted here so that stays a decision.
        keys = json.loads(answered[0]["result_ids_json"] or "[]")
        assert keys, "an answered search must record the ids it returned"
        resolved = client.get("/nodes/content", params={"node_id": keys[0]})
        assert resolved.status_code == 200, keys[0]
        assert "pheasant-flock" in (resolved.json()["content"] or ""), (
            f"the key {keys[0]} does not address the text the search returned"
        )

        # -- 6. rolled out of /state into cold Parquet ---------------------------
        state.execute(
            "UPDATE interaction_events SET started_at=?", ("2020-03-04T05:06:07.000000Z",)
        )
        with _Phase("roll"):
            report = lq.roll(
                state, region["config"].observability.interactions, exports_path=root / "exports"
            )
        assert report["rolled"] == hot
        assert report["disposition"] == "cold"
        assert lq.hot_row_count(state) == 0, "the hot store must be emptied by the roll"
        partitions = sorted((root / "exports" / "interactions").glob("dt=*"))
        assert [p.name for p in partitions] == ["dt=2020-03-04"]

        # -- 7. an outside reader gets it back out of cold storage ---------------
        duckdb = pytest.importorskip("duckdb")
        with _Phase("cold_read"):
            connection = duckdb.connect(":memory:")
            cold = connection.execute(
                "SELECT count(*), count(DISTINCT session_id), count(DISTINCT trace_id) "
                f"FROM read_parquet('{(root / 'exports').as_posix()}/interactions/**/*.parquet')"
            ).fetchone()
        assert cold[0] == hot
        assert cold[1] == 2, "both sessions survived the round trip through Parquet"
        assert cold[2] == 2, "both traces survived it too"

        # -- 8. the ledger becomes memory ----------------------------------------
        # Replay cold back into the hot window. Formation reads hot, and step 5
        # emptied it on purpose to prove the roll actually moves rows -- so this
        # doubles as the assertion that a cold partition is a complete, faithful
        # copy: every ledger column, round-tripped through Parquet, reinserted.
        #
        # Named columns rather than `SELECT *`: DuckDB adds a `dt` column from the
        # hive partition, and asking for exactly the schema's columns is also how
        # this notices a cold file that has silently stopped carrying one.
        from pheasant.telemetry.interactions import COLUMNS

        replayed = connection.execute(
            f"SELECT {','.join(COLUMNS)} FROM read_parquet("
            f"'{(root / 'exports').as_posix()}/interactions/**/*.parquet')"
        ).fetchall()
        assert len(replayed) == hot
        with state.conn:
            for record in replayed:
                state.conn.execute(
                    f"INSERT INTO interaction_events({','.join(COLUMNS)}) "
                    f"VALUES({','.join('?' for _ in COLUMNS)}) ON CONFLICT (id) DO NOTHING",
                    tuple(record),
                )
        assert lq.hot_row_count(state) == hot

        with _Phase("formation"):
            digests = run_session_digests(engine)
            candidates = run_candidate_rules(engine)
        assert len(digests["created"]) == 2, "one digest per session"
        assert candidates["open"] >= 1, "the rules proposed nothing from six real searches"

        proposals = state.list_memory_candidates()
        gaps = [c for c in proposals if c["rule_id"] == "retrieval-gap-v1"]
        assert gaps, "a question nothing answered should have been noticed"

        # -- 9. promotion is the one crossing into memory ------------------------
        with _Phase("promote"):
            promoted = admit(engine, gaps[0]["id"], admitted_by="user:ada")
        store = MemoryStore(memory_source(region["config"], state).path)
        record = next(r for r in store.list_records() if r.record_id == promoted["record_id"])
        assert "vault seal rotation" in record.text
        assert "formed" in record.tags

        # -- 10. it is an ordinary record, indexed by the ordinary pipeline -------
        with _Phase("index_memory"):
            engine.sync_source("agent-memory", "full")
        projected = state.rows(
            "SELECT record_id, scope, kind FROM memory_records WHERE record_id=?",
            (promoted["record_id"],),
        )
        assert projected, "a promoted record must reach the projection like any other"

        # -- 11. and a search finds it -------------------------------------------
        with _Phase("recall"):
            found = client.post(
                "/search",
                json={"query": "vault seal rotation", "mode": "hybrid", "max_results": 10},
                headers={"X-Pheasant-Session": "sess-c"},
            )
        assert found.status_code == 200
        hits = found.json()["results"]
        memories = [hit for hit in hits if hit.get("memory")]
        assert memories, "no formed memory was returned by search"
        # Among the hits, not necessarily first: the session digests also name
        # this question (under "Found nothing for"), so several formed records
        # legitimately match. What matters is that the promoted one made the
        # whole trip and comes back.
        returned = {hit["memory"]["record_id"] for hit in memories}
        assert promoted["record_id"] in returned, (
            f"the promoted record is not in {sorted(returned)}"
        )
        promoted_hit = next(
            hit for hit in memories if hit["memory"]["record_id"] == promoted["record_id"]
        )
        assert promoted_hit["memory"]["scope"] == "org"
        # Labelled as a remembered assertion rather than passed off as a
        # document -- the distinction the answering prompt depends on.
        assert promoted_hit["memory"]["asserted_at"]

        _report()


def _report() -> None:
    """Print the phase timings, and write them where CI can pick them up."""

    total = round(sum(TIMINGS.values()), 1)
    lines = [f"{name}={value}ms" for name, value in TIMINGS.items()]
    print(f"\nmemory round trip: {' '.join(lines)} total={total}ms")
    target = os.environ.get("PHEASANT_ROUNDTRIP_REPORT")
    if target:
        Path(target).write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(UTC)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                    "phases_ms": TIMINGS,
                    "total_ms": total,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
