"""The observation plane: what gets recorded, and what gets dropped instead.

The load-bearing claims here are the ones a reviewer would otherwise have to
take on trust:

* with observation off, nothing changes anywhere (CLAUDE.md rule 7);
* the request path never blocks and never raises, whatever the sink does;
* under pressure the tier loses **data**, not latency, and says so in a metric;
* an observation is never a memory record, and nothing here writes one.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence.state_store import StateStore
from pheasant.telemetry.interactions import (
    COLUMNS,
    INSERT_SQL,
    InteractionBuffer,
    InteractionContext,
    InteractionEvent,
    Modality,
    NullSink,
    SpoolSink,
    StateSink,
    configure_tracing,
    event_id,
    observe,
    parse_traceparent,
    process_buffer,
    redact,
    resolve_sink,
    set_process_buffer,
)
from pheasant.telemetry.metrics import REGISTRY, register_default_metrics


@pytest.fixture(autouse=True)
def _metrics() -> None:
    register_default_metrics("0.0.0-test")


@pytest.fixture(autouse=True)
def _isolate_tracing() -> Any:
    """`TRACING` is process-wide, so a test that configures it must not leave
    it configured for the next one -- which would silently turn an offline
    assertion into one that depends on whatever the previous test attached."""

    from pheasant.telemetry import interactions as module

    previous = (module.TRACING.tracer, module.TRACING.enabled)
    yield
    module.TRACING.tracer, module.TRACING.enabled = previous


@pytest.fixture
def state(tmp_path: Path) -> StateStore:
    store = StateStore(tmp_path / "p.db")
    store.migrate()
    return store


def _event(index: int = 0, **kwargs: Any) -> InteractionEvent:
    payload: dict[str, Any] = {
        "kb_id": "kb",
        "operation": "search_context",
        "trace_id": f"{index:032x}",
        "span_id": f"{index:016x}",
        "started_at": "2026-01-01T00:00:00.000000Z",
    }
    payload.update(kwargs)
    return InteractionEvent(**payload)


def _dropped(reason: str) -> float:
    return REGISTRY.value("pheasant_interaction_events_dropped_total", reason=reason) or 0.0


# --------------------------------------------------------------------------
# Identity, trace correlation, and the row
# --------------------------------------------------------------------------


def test_an_inbound_traceparent_is_adopted_so_one_call_is_one_trace() -> None:
    """An agent's own trace must not become a second, unrelated one."""

    context = InteractionContext.create(
        Modality.MCP, traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    )

    assert context.trace_id == "a" * 32
    assert context.parent_span_id == "b" * 16
    assert context.span_id not in ("", "b" * 16)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "garbage",
        "00-tooshort-" + "b" * 16 + "-01",
        # All-zero ids are reserved as invalid by the spec. Accepting one
        # would collapse every such caller into a single shared trace.
        "00-" + "0" * 32 + "-" + "b" * 16 + "-01",
        "00-" + "a" * 32 + "-" + "0" * 16 + "-01",
    ],
)
def test_an_unusable_traceparent_starts_a_fresh_trace_rather_than_failing(header: str) -> None:
    assert parse_traceparent(header) is None
    context = InteractionContext.create(Modality.UI, traceparent=header)
    assert len(context.trace_id) == 32
    assert context.parent_span_id is None


def test_an_unknown_modality_is_data_not_a_crash() -> None:
    """This is reached from a header on an unauthenticated API."""

    assert InteractionContext.create("not-a-surface").modality is Modality.UI


def test_the_row_id_is_content_addressed_on_the_span() -> None:
    """What makes at-least-once redelivery a no-op instead of a duplicate."""

    assert event_id("a" * 32, "b" * 16) == event_id("a" * 32, "b" * 16)
    assert event_id("a" * 32, "b" * 16) != event_id("a" * 32, "c" * 16)


def test_the_row_matches_the_insert_it_is_written_with() -> None:
    """Three column lists that could drift are derived from one tuple."""

    assert len(_event().as_row()) == len(COLUMNS)
    assert INSERT_SQL.count("?") == len(COLUMNS)
    for name in COLUMNS:
        assert name in INSERT_SQL


def test_an_event_round_trips_through_json_for_the_queue() -> None:
    event = _event(query_text="where is the watcher", result_ids=["chunk:a"], top_score=0.9)
    restored = InteractionEvent.from_json(json.loads(json.dumps(event.as_json())))
    assert restored.as_row() == event.as_row()


# --------------------------------------------------------------------------
# The request path
# --------------------------------------------------------------------------


def test_observe_fills_in_timing_and_hands_the_event_over() -> None:
    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    context = InteractionContext.create(Modality.MCP, principal="user:ada", session_id="s1")

    with observe(buffer, context, kb_id="kb", operation="search_context") as event:
        event.result_ids = ["chunk:a", "chunk:b"]

    assert buffer.depth == 1
    recorded = buffer._drain(1)[0]
    assert recorded.principal == "user:ada"
    assert recorded.session_id == "s1"
    assert recorded.modality == "mcp"
    assert recorded.status == "ok"
    assert recorded.result_count == 2
    assert recorded.duration_ms is not None


def test_a_failing_handler_is_recorded_as_an_error_and_re_raised_unchanged() -> None:
    """Observation never swallows a caller's failure, and never invents one."""

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    context = InteractionContext.create(Modality.UI)

    with pytest.raises(ValueError, match="Unknown source: typo"):
        with observe(buffer, context, kb_id="kb", operation="sync_source"):
            raise ValueError("Unknown source: typo")

    assert buffer._drain(1)[0].status == "error"


def test_a_broken_sink_never_reaches_the_caller() -> None:
    class Exploding:
        name = "exploding"

        def write(self, events: Any) -> int:
            raise RuntimeError("the database is on fire")

        def close(self) -> None:
            return None

    buffer = InteractionBuffer(Exploding(), capacity=10, batch_size=1, interval_seconds=99)
    before = _dropped("error")

    with observe(buffer, InteractionContext.create(Modality.UI), kb_id="kb", operation="x"):
        pass
    assert buffer.flush() == 0

    assert _dropped("error") > before


# --------------------------------------------------------------------------
# Backpressure — the invariant
# --------------------------------------------------------------------------


def test_a_full_buffer_drops_the_oldest_and_counts_it() -> None:
    """Bounded, and it says so. Under sustained overload the recent past is
    the more useful half, so the newest event is the one kept."""

    buffer = InteractionBuffer(NullSink(), capacity=2, batch_size=99, interval_seconds=99)
    before = _dropped("buffer_full")

    accepted = [buffer.record(_event(index)) for index in range(5)]

    assert buffer.depth == 2
    assert accepted[:2] == [True, True]
    assert accepted[2:] == [False, False, False]
    assert _dropped("buffer_full") - before == 3
    # The two survivors are the newest, not the oldest.
    assert [event.trace_id for event in buffer._drain(2)] == [f"{3:032x}", f"{4:032x}"]


def test_a_drowning_queue_is_not_published_into() -> None:
    """Otherwise a stalled log tier turns a bounded buffer into an unbounded
    table -- the same failure wearing a different hat."""

    from pheasant.telemetry.interactions import QueueSink

    class Drowning:
        def depth(self) -> dict[str, int]:
            return {"pending": 10_000, "inflight": 0}

        def publish_batch(self, task_id: str, payload: dict) -> None:  # pragma: no cover
            raise AssertionError("must not publish into a queue past its depth limit")

    sink = QueueSink(Drowning(), kb_id="kb", max_depth=100)
    before = _dropped("queue_full")

    assert sink.write([_event()]) == 0
    assert _dropped("queue_full") - before == 1


def test_an_unreadable_queue_depth_is_not_a_reason_to_stop_recording() -> None:
    from pheasant.telemetry.interactions import QueueSink

    published: list[str] = []

    class Flaky:
        def depth(self) -> dict[str, int]:
            raise RuntimeError("no")

        def publish_batch(self, task_id: str, payload: dict) -> None:
            published.append(task_id)

    assert QueueSink(Flaky(), kb_id="kb", max_depth=1).write([_event()]) == 1
    assert len(published) == 1


# --------------------------------------------------------------------------
# Sink selection — a capability probe, not a config switch
# --------------------------------------------------------------------------


def test_sink_selection_follows_what_this_process_can_actually_do(state: StateStore) -> None:
    settings = PheasantConfig().observability.interactions

    queue = type("Q", (), {"publish_batch": lambda self, i, p: None, "depth": lambda self: {}})()
    assert resolve_sink(settings, state=state, queue=queue, kb_id="kb").name == "queue"
    assert resolve_sink(settings, state=state).name == "state"
    # `/state:ro` on an API replica under SQLite: there is nowhere to write.
    assert resolve_sink(settings, state=state, state_writable=False).name == "null"
    assert resolve_sink(settings, state=None).name == "null"


def test_a_read_only_replica_spools_when_told_where(tmp_path: Path, state: StateStore) -> None:
    settings = PheasantConfig().observability.interactions
    settings.spool_path = tmp_path / "spool"

    sink = resolve_sink(settings, state=state, state_writable=False, owner="api/2")
    assert isinstance(sink, SpoolSink)
    assert sink.write([_event(1), _event(2)]) == 2

    files = list((tmp_path / "spool").rglob("*.ndjson"))
    assert len(files) == 1
    # The owner is part of the path so two replicas cannot interleave lines.
    assert "api-2" in str(files[0])
    assert len(files[0].read_text().strip().splitlines()) == 2


def test_a_null_sink_warns_once_rather_than_per_request(caplog: Any) -> None:
    sink = NullSink("state_read_only")
    with caplog.at_level("WARNING"):
        for _ in range(5):
            sink.write([_event()])
    assert sum("nothing here can persist it" in record.message for record in caplog.records) == 1


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def test_writing_the_same_span_twice_leaves_one_row(state: StateStore) -> None:
    """At-least-once redelivery is the queue's normal mode, not an error."""

    sink = StateSink(state)
    sink.write([_event(1), _event(2)])
    sink.write([_event(1), _event(2)])

    assert state.rows("SELECT COUNT(*) AS c FROM interaction_events", ())[0]["c"] == 2


def test_an_observation_is_never_a_memory_record(state: StateStore) -> None:
    """The boundary the whole design rests on."""

    StateSink(state).write([_event(1), _event(2)])

    assert state.rows("SELECT COUNT(*) AS c FROM memory_records", ())[0]["c"] == 0
    assert state.rows("SELECT COUNT(*) AS c FROM artifacts", ())[0]["c"] == 0
    assert state.rows("SELECT COUNT(*) AS c FROM chunks", ())[0]["c"] == 0


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_redaction_drops_question_and_answer_together() -> None:
    """Redacting the question while keeping an answer that quotes the corpus
    back at it would be incoherent, which is why the setting is `redact_text`
    and not `redact_query_text`."""

    event = redact(
        _event(
            criteria={"mode": "hybrid"},
            query_text="who owns billing",
            answer_text="Billing is owned by the finance service [1].",
            result_ids=["chunk:a"],
            result_paths=["finance/billing.md"],
            principal="user:ada",
            session_id="s1",
        ),
        enabled=True,
    )

    assert event.query_text is None
    assert event.answer_text is None
    assert event.attributes["text_redacted"] is True
    # Everything `path-affinity-v1` and `retrieval-gap-v1` count on survives.
    assert event.criteria == {"mode": "hybrid"}
    assert event.result_ids == ["chunk:a"]
    assert event.result_paths == ["finance/billing.md"]
    # Identity is deliberately *not* what this knob redacts: it is what scopes
    # a formed memory, and dropping it would make every observation org-wide.
    assert (event.principal, event.session_id) == ("user:ada", "s1")


def test_redaction_off_leaves_the_text_alone() -> None:
    event = redact(_event(query_text="q", answer_text="a"), enabled=False)
    assert (event.query_text, event.answer_text) == ("q", "a")


def test_an_answer_is_capped_and_the_cut_is_recorded() -> None:
    """A rule counting phrases in an answer must be able to tell a short
    answer from a clipped one."""

    from pheasant.telemetry.interactions import cap_answer

    short = cap_answer(_event(answer_text="brief"), max_chars=100)
    assert short.answer_text == "brief"
    assert "answer_truncated" not in short.attributes

    long = cap_answer(_event(answer_text="x" * 500), max_chars=100)
    assert len(long.answer_text) == 100
    assert long.attributes["answer_truncated"] is True


def test_a_zero_cap_records_no_answers_at_all() -> None:
    """`0` means off, the same shape `hot_retention_days` and
    `supersede_retention_days` already use."""

    from pheasant.telemetry.interactions import cap_answer

    assert cap_answer(_event(answer_text="anything"), max_chars=0).answer_text is None
    assert PheasantConfig().observability.interactions.max_answer_chars == 4000


# --------------------------------------------------------------------------
# Extracting results: one grammar, both surfaces
# --------------------------------------------------------------------------


def test_results_split_into_ids_and_paths_in_the_grammars_rules_need() -> None:
    """Ids join to graph_nodes and chunks; paths are `relative_path`, which is
    what `steering` matches against -- so a `preference` rule minted from
    these can actually fire."""

    from pheasant.telemetry.interactions import extract_results

    ids, paths, top, count = extract_results(
        {
            "results": [
                {"node_id": "file:docs:a.md:branch=none", "relative_path": "a.md", "score": 0.9},
                {
                    "chunk_id": "chunk:docs:b.md:sha256=x:chunk=0",
                    "relative_path": "b.md",
                    "score": 0.4,
                },
            ]
        }
    )

    assert ids == ["file:docs:a.md:branch=none", "chunk:docs:b.md:sha256=x:chunk=0"]
    assert paths == ["a.md", "b.md"]
    assert top == 0.9
    assert count == 2


@pytest.mark.parametrize("key", ["results", "files", "citations"])
def test_every_surface_shape_is_understood(key: str) -> None:
    """`/search` answers with `results`, `/relevant-files` with `files`, chat
    with `citations`. A rule must not care which."""

    from pheasant.telemetry.interactions import extract_results

    ids, paths, _top, count = extract_results({key: [{"node_id": "n1", "relative_path": "p.md"}]})
    assert (ids, paths, count) == (["n1"], ["p.md"], 1)


def test_a_bare_path_list_still_yields_paths() -> None:
    from pheasant.telemetry.interactions import extract_results

    assert extract_results({"files": ["a.md", "b.md"]})[1] == ["a.md", "b.md"]


def test_extraction_never_raises_on_a_shape_it_does_not_know() -> None:
    """27 tools return 27 shapes; a rule that covers what it knows beats a
    crash on the rest."""

    from pheasant.telemetry.interactions import extract_results

    assert extract_results(None) == ([], [], None, 0)
    assert extract_results({"nothing": "useful"}) == ([], [], None, 0)
    assert extract_results({"results": "not a list"}) == ([], [], None, 0)
    assert extract_results({"results": [None, 42, {"score": "high"}]}) == ([], [], None, 3)


def test_a_recorded_result_list_is_capped() -> None:
    """A `max_results: 500` query must not put 500 ids in one row; the count
    still reports the real total, which is what lets `retrieval-gap-v1` tell
    'nothing matched' from 'more than we recorded'."""

    from pheasant.telemetry.interactions import MAX_RESULTS_RECORDED, extract_results

    ids, paths, _top, count = extract_results(
        {"results": [{"node_id": f"n{i}", "relative_path": f"{i}.md"} for i in range(200)]}
    )
    assert len(ids) == len(paths) == MAX_RESULTS_RECORDED
    assert count == 200


def test_a_child_event_shares_its_parents_trace() -> None:
    """A streamed answer outlives the request that opened it, so it gets its
    own event rather than a late mutation of one already handed to the
    buffer -- and the trace is what joins them."""

    from pheasant.telemetry.interactions import child_event

    parent = _event(1, principal="user:ada", session_id="s1")
    child = child_event(parent, "/assistant/chat/stream")

    assert child.trace_id == parent.trace_id
    assert child.parent_span_id == parent.span_id
    assert child.span_id != parent.span_id
    assert (child.principal, child.session_id) == ("user:ada", "s1")
    assert child.operation == "/assistant/chat/stream"
    assert child.is_writable


# --------------------------------------------------------------------------
# Rule 7 — the no-infrastructure path
# --------------------------------------------------------------------------


def test_observation_is_off_by_default() -> None:
    settings = PheasantConfig().observability

    assert settings.interactions.enabled is False
    assert settings.interactions.queue.enabled is False
    assert settings.otlp_endpoint is None
    assert settings.interactions.cold_enabled is False


def test_no_otlp_endpoint_attaches_no_exporter_so_the_suite_stays_offline() -> None:
    """Network-freedom by construction rather than by mocking."""

    assert configure_tracing(PheasantConfig().observability) is False


def test_a_configured_endpoint_without_the_extra_degrades_rather_than_raising(
    caplog: Any,
) -> None:
    settings = PheasantConfig().observability
    settings.otlp_endpoint = "http://127.0.0.1:4318/v1/traces"

    with caplog.at_level("WARNING"):
        attached = configure_tracing(settings)

    try:
        # Either the extra is installed and it attaches, or it is not and we
        # say so -- never a crash, and never a silent no-op.
        assert attached in (True, False)
        if not attached:
            assert any("[otel] extra" in record.message for record in caplog.records)
    finally:
        # Nothing is exported here -- no span is created through this provider
        # -- but the batch processor it installed owns a thread, and leaving it
        # attached would make a later test's spans try to reach a collector
        # that is not there. The suite is offline by construction, and this is
        # the one place that could make it otherwise.
        if attached:
            from pheasant.telemetry.interactions import TRACING

            TRACING.shutdown()


def test_the_process_buffer_is_a_single_shared_slot() -> None:
    """So a mounted MCP app observes through the API's buffer instead of
    opening a second one that would double-count every /mcp call."""

    # A strict entry assertion on purpose: it is the canary for a test that
    # built an app without a lifespan (no TestClient), so the teardown that
    # clears this slot never ran. If this fails, the leak is in whatever ran
    # before it, not here.
    assert process_buffer() is None, (
        "another test left a process buffer behind: build the app inside a "
        "TestClient, or leave observability.interactions disabled"
    )
    buffer = InteractionBuffer(NullSink())
    try:
        set_process_buffer(buffer)
        assert process_buffer() is buffer
    finally:
        set_process_buffer(None)
    assert process_buffer() is None


# --------------------------------------------------------------------------
# OpenTelemetry, when the extra is installed
# --------------------------------------------------------------------------


@pytest.fixture
def otel_spans() -> Any:
    """A real SDK exporting into memory. Skipped without the [otel] extra."""

    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry import trace as ot_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from pheasant.telemetry import interactions as module

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    ot_trace.set_tracer_provider(provider)

    previous = (module.TRACING.tracer, module.TRACING.enabled)
    module.TRACING.tracer = provider.get_tracer("pheasant")
    module.TRACING.enabled = True
    try:
        yield exporter
    finally:
        module.TRACING.tracer, module.TRACING.enabled = previous


def test_a_ledger_row_and_its_exported_span_name_the_same_call(otel_spans: Any) -> None:
    """Most of the reason to export spans at all.

    The event is built with locally minted ids, because they are its primary
    key and must exist with or without the extra. When a real span is running,
    *its* ids are what the operator's collector shows -- so the row has to
    adopt them, or correlating a slow span to a row finds nothing.
    """

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)

    with observe(
        buffer, InteractionContext.create(Modality.MCP), kb_id="kb", operation="search_context"
    ):
        pass

    span = otel_spans.get_finished_spans()[0]
    event = buffer._drain(1)[0]
    assert event.trace_id == format(span.get_span_context().trace_id, "032x")
    assert event.span_id == format(span.get_span_context().span_id, "016x")


def test_an_inbound_trace_continues_into_the_exported_span(otel_spans: Any) -> None:
    """Otherwise the SDK starts its own root trace for a call the caller
    already had one for, and the collector shows two unrelated traces where
    there was one request."""

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    context = InteractionContext.create(
        Modality.MCP, traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    )

    with observe(buffer, context, kb_id="kb", operation="search_context"):
        pass

    span = otel_spans.get_finished_spans()[0]
    assert format(span.get_span_context().trace_id, "032x") == "a" * 32
    assert format(span.parent.span_id, "016x") == "b" * 16


def test_a_span_carries_the_shape_of_a_call_and_never_its_content(otel_spans: Any) -> None:
    """A collector is a different system with different retention. Query text
    and principal are the ledger's business, governed by `redact_query_text`
    there; they must not leak out through a span."""

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    context = InteractionContext.create(Modality.MCP, principal="user:ada", session_id="s1")

    with observe(buffer, context, kb_id="kb", operation="search_context") as event:
        event.query_text = "who owns billing"
        event.result_ids = ["chunk:a", "chunk:b"]

    attributes = dict(otel_spans.get_finished_spans()[0].attributes)
    assert attributes == {
        "pheasant.kb": "kb",
        "pheasant.modality": "mcp",
        "pheasant.operation": "search_context",
        "pheasant.result_count": 2,
    }
    serialized = str(attributes)
    assert "who owns billing" not in serialized
    assert "user:ada" not in serialized
    assert "s1" not in serialized


def test_a_failing_call_marks_its_span_as_an_error(otel_spans: Any) -> None:
    from opentelemetry.trace import StatusCode

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)

    with pytest.raises(ValueError):
        with observe(buffer, InteractionContext.create(Modality.UI), kb_id="kb", operation="boom"):
            raise ValueError("nope")

    assert otel_spans.get_finished_spans()[0].status.status_code is StatusCode.ERROR


def test_exporter_headers_come_from_an_environment_variable_never_the_config() -> None:
    """The config file names the variable; the value stays out of it. Same
    rule `storage.dsn_env` follows, for the same reason."""

    from pheasant.telemetry.interactions import _parse_headers

    assert _parse_headers("authorization=Bearer x,x-scope=team") == {
        "authorization": "Bearer x",
        "x-scope": "team",
    }
    assert _parse_headers("") == {}
    assert _parse_headers("nonsense") == {}
    assert PheasantConfig().observability.otlp_headers_env == "PHEASANT_OTLP_HEADERS"


# --------------------------------------------------------------------------
# The read-only /state case, which only exists in a container
# --------------------------------------------------------------------------


def test_a_read_only_state_is_detected_and_never_written(monkeypatch: Any, tmp_path: Path) -> None:
    """`docker-compose.scale.yml` mounts `/state:ro` on API replicas so the
    indexer is the only writer. Under SQLite that means an API replica must
    not try -- and it must not need an operator to have configured per-role
    what the mount already decided.

    The probe is exercised here by faking the filesystem answer; the real
    thing was checked against an actual read-only bind mount, which the kernel
    reports unwritable even to uid 0 (unlike bare permission bits, which root
    bypasses).
    """

    from pheasant.api.app import _state_is_writable

    config = PheasantConfig()
    config.storage.sqlite_path = tmp_path / "state" / "p.db"

    # `sys.modules`, not `import pheasant.api.app as ...`: the package's
    # __init__ binds `app = None`, which shadows the submodule attribute.
    import sys

    monkeypatch.setattr(sys.modules["pheasant.api.app"].os, "access", lambda path, mode: False)
    assert _state_is_writable(config, object()) is False

    # Postgres does not care what the state volume allows: the ledger is in
    # the database, which is why the shipped fleet needs no spool at all.
    config.storage.backend = "postgres"
    assert _state_is_writable(config, object()) is True


def test_a_writable_state_is_detected(tmp_path: Path) -> None:
    from pheasant.api.app import _state_is_writable

    config = PheasantConfig()
    config.storage.sqlite_path = tmp_path / "p.db"
    assert _state_is_writable(config, object()) is True


# --------------------------------------------------------------------------
# The HTTP surface records content, not just a path and a status
# --------------------------------------------------------------------------


def _app_with_corpus(tmp_path: Path, **interactions: Any) -> Any:
    import yaml
    from fastapi.testclient import TestClient  # noqa: F401  (imported by callers)

    from pheasant.api.app import create_app

    docs = tmp_path / "ws" / "docs"
    docs.mkdir(parents=True)
    (docs / "runbook.md").write_text(
        "# Kestrel Runbook\n\nThe filewatch daemon restarts nightly at 0300 UTC.\n",
        encoding="utf-8",
    )
    for name in ("state", "exports"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    raw = {
        "pheasant": {
            "name": "kb",
            "state_path": str(tmp_path / "state"),
            "workspace_root": str(tmp_path / "ws"),
            "exports_path": str(tmp_path / "exports"),
        },
        "storage": {"graph_snapshots": False},
        "observability": {"interactions": {"enabled": True, "flush_batch_size": 1, **interactions}},
        "sources": [
            {
                "name": "docs",
                "type": "markdown_folder",
                "path": str(docs),
                "include": ["**/*.md"],
                # Indexed explicitly below instead: the startup sync runs on an
                # executor, so a request made straight after `TestClient(...)`
                # would race it and search an empty index.
                "sync": {"on_startup": False},
            }
        ],
    }
    path = tmp_path / "pheasant.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    app = create_app(PheasantConfig.model_validate(raw), config_path=str(path))
    app.state.engine.sync_source("docs", "full")
    return app


def test_a_ui_search_records_the_question_and_what_came_back(tmp_path: Path) -> None:
    """The gap this closes: the middleware cannot read a request body without
    consuming the stream, so before handlers enriched the event every UI
    search recorded a path and a status and no content -- and three of the
    four formation rules would have worked for MCP agents only, on the very
    surface plan 2 was written about.
    """

    from fastapi.testclient import TestClient

    app = _app_with_corpus(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/search",
            json={"query": "filewatch daemon nightly", "mode": "hybrid", "max_results": 5},
            headers={"X-Pheasant-Session": "sess-7", "X-Pheasant-Principal": "user:ada"},
        )
        assert response.status_code == 200
        app.state.interaction_buffer.flush()

    row = dict(
        app.state.state.rows(
            "SELECT query_text, criteria_json, result_ids_json, result_paths_json, "
            "result_count, top_score, session_id, principal, modality "
            "FROM interaction_events WHERE operation='/search'",
            (),
        )[0]
    )
    assert row["query_text"] == "filewatch daemon nightly"
    assert row["session_id"] == "sess-7"
    assert row["principal"] == "user:ada"
    assert row["modality"] == "ui"
    assert '"mode": "hybrid"' in row["criteria_json"]
    # Stable ids join to the graph; paths are source-relative, the grammar a
    # steering `preference` rule matches against.
    assert "runbook.md" in json.loads(row["result_paths_json"])
    assert json.loads(row["result_ids_json"])[0].startswith("file:docs:")
    assert row["result_count"] >= 1
    assert row["top_score"] is not None


def test_a_streamed_answer_is_a_child_of_the_request_that_opened_it(tmp_path: Path) -> None:
    """The route returns its response object before the answer exists, so the
    request's event is already buffered by then. The answer gets its own event
    under the same trace rather than a racing late mutation of that one."""

    import time

    from fastapi.testclient import TestClient

    app = _app_with_corpus(tmp_path)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/assistant/chat/stream",
            json={"question": "what restarts nightly?"},
            headers={"X-Pheasant-Session": "sess-7"},
        ) as response:
            for _ in response.iter_lines():
                pass
        time.sleep(1.0)
        app.state.interaction_buffer.flush()

    rows = [
        dict(row)
        for row in app.state.state.rows(
            "SELECT operation, trace_id, span_id, parent_span_id, query_text, answer_text "
            "FROM interaction_events ORDER BY started_at",
            (),
        )
    ]
    parent = next(row for row in rows if row["operation"] == "/assistant")
    child = next(row for row in rows if row["operation"] == "/assistant/chat/stream")

    assert child["trace_id"] == parent["trace_id"]
    assert child["parent_span_id"] == parent["span_id"]
    assert child["query_text"] == "what restarts nightly?"
    assert child["answer_text"]
    # The parent carries no content on purpose: the middleware never sees the
    # body, and the answer did not exist when its span closed. Formation reads
    # rows with a question, which is exactly the child.
    assert parent["query_text"] is None


# --------------------------------------------------------------------------
# Timestamps and traces: the two things every row must have
# --------------------------------------------------------------------------


def test_every_row_carries_a_trace_and_a_timestamp() -> None:
    """The schema says NOT NULL on both; `is_writable` is what keeps a batch
    from failing wholesale on a row that lacks them."""

    from pheasant.persistence.schema import CORE_SCHEMA

    table = CORE_SCHEMA[CORE_SCHEMA.index("CREATE TABLE IF NOT EXISTS interaction_events") :]
    table = table[: table.index(");")]
    for column in ("trace_id", "span_id", "started_at", "status"):
        assert f"{column} TEXT NOT NULL" in table, f"{column} must be NOT NULL"
    # A root span genuinely has no parent, so this one is nullable on purpose.
    assert "parent_span_id TEXT," in table


def test_a_duration_is_always_recorded() -> None:
    """`duration_ms` is the one column an operator reads to find slow calls.
    A null there is a row that cannot answer the question it exists for."""

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)

    with observe(buffer, InteractionContext.create(Modality.UI), kb_id="kb", operation="/search"):
        pass
    with pytest.raises(ValueError):
        with observe(buffer, InteractionContext.create(Modality.UI), kb_id="kb", operation="x"):
            raise ValueError("boom")

    recorded = buffer._drain(2)
    assert len(recorded) == 2
    # Including the failed call: how long a request took before it failed is
    # exactly as interesting as how long a successful one took.
    assert all(event.duration_ms is not None and event.duration_ms >= 0 for event in recorded)


def test_durations_come_from_a_monotonic_clock() -> None:
    """Subtracting two wall-clock readings makes an NTP step mid-request emit
    a negative or wildly inflated duration -- nonsense in the one column an
    operator reads to find slow calls."""

    import pheasant.telemetry.interactions as module

    source = inspect.getsource(module.observe)
    assert "time.perf_counter()" in source
    assert "utc_now() - started" not in source


def test_a_trace_is_ambient_only_for_the_duration_of_a_call() -> None:
    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    from pheasant.telemetry.interactions import current_trace, traceparent_header

    assert current_trace() is None
    with observe(buffer, InteractionContext.create(Modality.UI), kb_id="kb", operation="/search"):
        inside = traceparent_header()
        assert inside is not None
        assert current_trace() is not None
    assert current_trace() is None
    assert traceparent_header() is None


def test_an_outbound_hop_carries_the_callers_trace() -> None:
    """Without this a trace stops dead at pheasant's own boundary: an operator
    sees "search took four seconds" and cannot see that most of it was one
    graph-query call to another pod."""

    from pheasant.telemetry.interactions import inject_traceparent

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    context = InteractionContext.create(
        Modality.MCP, traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    )

    with observe(buffer, context, kb_id="kb", operation="/search"):
        headers = inject_traceparent({"Authorization": "Bearer x"})

    assert headers["Authorization"] == "Bearer x"
    # Same trace as the caller's, a new span for our own work.
    assert headers["traceparent"].startswith("00-" + "a" * 32 + "-")
    assert "b" * 16 not in headers["traceparent"]


def test_no_ambient_trace_means_no_header_rather_than_an_empty_one() -> None:
    """Every sync is in this state today. A blank or zeroed traceparent would
    be worse than none: the spec reserves all-zero ids as invalid, and a
    collector would collapse every such hop into one shared trace."""

    from pheasant.telemetry.interactions import inject_traceparent

    assert inject_traceparent({"a": "1"}) == {"a": "1"}


def test_a_queued_task_carries_the_trace_of_the_request_that_queued_it() -> None:
    """`POST /sync` and the indexer that runs it, minutes later in another
    process, must be one trace -- not two unrelated ones for one thing a
    person asked for."""

    from pheasant.telemetry.interactions import adopt_trace, traceparent_header

    published = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"

    assert traceparent_header() is None
    with adopt_trace(published):
        assert traceparent_header() == published
        # And every hop the indexer makes onward inherits it.
        from pheasant.telemetry.interactions import inject_traceparent

        assert inject_traceparent({})["traceparent"] == published
    assert traceparent_header() is None


@pytest.mark.parametrize("value", [None, "", "garbage", "00-" + "0" * 32 + "-" + "d" * 16 + "-01"])
def test_an_unusable_queued_trace_is_no_trace_never_a_failure(value: str) -> None:
    """A sync must never fail because a header was malformed."""

    from pheasant.telemetry.interactions import adopt_trace, traceparent_header

    with adopt_trace(value):
        assert traceparent_header() is None


def test_the_trace_a_row_carries_is_the_one_hops_propagate(otel_spans: Any) -> None:
    """With the SDK running, the row adopts the SDK's ids -- so the ambient
    trace has to be re-published, or an outbound hop would carry the ids we
    minted before the SDK spoke and the collector would show two traces."""

    from pheasant.telemetry.interactions import inject_traceparent

    buffer = InteractionBuffer(NullSink(), capacity=10, batch_size=99, interval_seconds=99)
    with observe(buffer, InteractionContext.create(Modality.MCP), kb_id="kb", operation="/search"):
        propagated = inject_traceparent({})["traceparent"]

    event = buffer._drain(1)[0]
    span = otel_spans.get_finished_spans()[0]
    assert propagated == f"00-{event.trace_id}-{event.span_id}-01"
    assert event.trace_id == format(span.get_span_context().trace_id, "032x")


def test_an_event_without_a_trace_is_counted_not_silently_dropped() -> None:
    """A defect that only ever shows up as a slightly smaller ledger is a
    defect nobody finds."""

    from pheasant.telemetry.interactions import events_from_batch

    before = _dropped("malformed")
    events = events_from_batch(
        {
            "events": [
                _event(1).as_json(),
                {"kb_id": "kb", "operation": "x"},  # no trace, no timestamp
                "not-an-event",
            ]
        }
    )

    assert len(events) == 1
    assert _dropped("malformed") - before == 2


def test_liveness_probes_are_not_interactions(tmp_path: Path) -> None:
    """A health check is machine chatter: no query, no session, no principal,
    and nothing a formation rule could read.

    Recording them is not merely useless, it is expensive. A container probe
    every 3s is 28,800 rows per replica per day; a three-replica fleet holding
    a week would carry half a gigabyte of them. Found by reading a CI log-tier
    run where the row count climbed between assertions with no traffic but the
    probes.
    """

    from fastapi.testclient import TestClient

    from pheasant.api.app import UNOBSERVED_PATHS

    app = _app_with_corpus(tmp_path)
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code in (200, 503)
            assert client.get("/metrics").status_code == 200
        # One real call, so this cannot pass by observing nothing at all.
        client.post("/search", json={"query": "filewatch daemon", "max_results": 3})
        app.state.interaction_buffer.flush()

    operations = [
        str(row["operation"])
        for row in app.state.state.rows("SELECT operation FROM interaction_events", ())
    ]
    assert operations == ["/search"], f"probes leaked into the ledger: {operations}"
    assert {"/health", "/ready", "/metrics"} <= UNOBSERVED_PATHS


def test_the_mcp_mount_is_unobserved_at_the_http_layer() -> None:
    """It is observed one level down, by the decorator that wraps every tool,
    which knows the tool name and its criteria. Counting both would double
    every formation threshold."""

    from pheasant.api.app import UNOBSERVED_PATHS

    assert "/mcp" in UNOBSERVED_PATHS
