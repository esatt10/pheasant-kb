"""Exercise real HTTP/gRPC OTLP export and correlation, including privacy bounds."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pheasant.config.schema import ObservabilitySettings
from pheasant.telemetry.tracing import Tracing, destination


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    import os

    for key in list(os.environ):
        if key.startswith("OTEL_") or key.startswith("CONTAINERAPP_OTEL"):
            monkeypatch.delenv(key)


@pytest.mark.parametrize("protocol", ["grpc", "http/protobuf"])
def test_standard_environment_and_explicit_override(monkeypatch, protocol):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://agent:4317/")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol)
    suffix = "/v1/traces" if protocol == "http/protobuf" else "/"
    assert destination(ObservabilitySettings())[:2] == ("http://agent:4317" + suffix, protocol)
    assert destination(ObservabilitySettings(otlp_endpoint="https://custom/v1/traces"))[:2] == (
        "https://custom/v1/traces",
        "http/protobuf",
    )
    assert (
        destination(ObservabilitySettings(otlp_endpoint="https://custom/v1/traces"), "logs")[0]
        == "https://custom/v1/logs"
    )


def test_signal_endpoints_and_headers(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://base")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "https://trace/special")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "a=1,token=x=2")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "a=2")
    monkeypatch.setenv("PHEASANT_OTLP_HEADERS", "b=3")
    assert destination(ObservabilitySettings()) == (
        "https://trace/special",
        "http/protobuf",
        {"a": "2", "b": "3", "token": "x=2"},
    )


def test_disabled_has_no_provider_or_background_threads():
    tracing = Tracing()
    assert tracing.configure(ObservabilitySettings()) is False
    assert tracing.provider is None and tracing.logs is None


@pytest.fixture(params=["grpc", "http/protobuf"])
def collector(request):
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
        ExportLogsServiceRequest,
        ExportLogsServiceResponse,
    )
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
        ExportTraceServiceResponse,
    )

    traces, logs = [], []
    if request.param == "grpc":
        grpc = pytest.importorskip("grpc")
        from opentelemetry.proto.collector.logs.v1 import logs_service_pb2_grpc
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

        class Trace(trace_service_pb2_grpc.TraceServiceServicer):
            def Export(self, payload, context):
                traces.append(payload)
                return ExportTraceServiceResponse()

        class Logs(logs_service_pb2_grpc.LogsServiceServicer):
            def Export(self, payload, context):
                logs.append(payload)
                return ExportLogsServiceResponse()

        server = grpc.server(ThreadPoolExecutor(max_workers=2))
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Trace(), server)
        logs_service_pb2_grpc.add_LogsServiceServicer_to_server(Logs(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        yield f"http://127.0.0.1:{port}", request.param, traces, logs
        server.stop(0).wait()
    else:

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["content-length"]))
                if self.path == "/v1/traces":
                    traces.append(ExportTraceServiceRequest.FromString(raw))
                elif self.path == "/v1/logs":
                    logs.append(ExportLogsServiceRequest.FromString(raw))
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield f"http://127.0.0.1:{server.server_port}", request.param, traces, logs
        server.shutdown()
        server.server_close()
        thread.join()


def flattened(traces):
    return [
        span
        for batch in traces
        for resource in batch.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]


def test_real_wire_export_has_shared_ids_logs_and_service(monkeypatch, collector):
    from pheasant.telemetry import interactions
    from pheasant.telemetry.interactions import InteractionContext, observe, traceparent_header
    from pheasant.telemetry.spans import span
    from pheasant.telemetry.sync_trace import SyncTrace, emit

    endpoint, protocol, traces, logs = collector
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "pheasant-api")
    monkeypatch.setenv("PHEASANT_ROLE", "api")
    tracing = Tracing()
    monkeypatch.setattr(interactions, "TRACING", tracing)
    try:
        assert tracing.configure(ObservabilitySettings())
        provider = tracing.provider
        assert tracing.configure(ObservabilitySettings()) and tracing.provider is provider
        inbound = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
        with observe(
            None, InteractionContext.create(traceparent=inbound), kb_id="test", operation="/search"
        ) as event:
            from pheasant.mcp_server.server import _anticipated_failures

            @_anticipated_failures("tool")
            def list_sources(knowledge_base):
                return {"sources": []}

            assert list_sources(knowledge_base="test") == {"sources": []}
            with span("postgresql.rows", kind="CLIENT", attributes={"db.system": "postgresql"}):
                assert traceparent_header().startswith("00-" + "a" * 32)
            run = SyncTrace("test", "incremental")
            with run:
                with span("grpc.post", kind="CLIENT"):
                    pass
            recorded = run.finish("ok", {})
            emit(recorded)
            logging.getLogger("pheasant.telemetry.test").warning(
                "wire export Bearer private-token ?api_key=private-key"
            )
        tracing.flush()
        result = flattened(traces)
        root = next(s for s in result if s.name == "/search")
        assert root.trace_id.hex() == event.trace_id == "a" * 32
        assert root.span_id.hex() == event.span_id and root.parent_span_id.hex() == "b" * 16
        assert root.kind == 2  # SERVER -> Application Insights requests.
        tool = next(s for s in result if s.name == "list_sources")
        assert tool.parent_span_id == root.span_id and tool.trace_id == root.trace_id
        dependency = next(s for s in result if s.name == "postgresql.rows")
        assert dependency.kind == 3 and dependency.parent_span_id == root.span_id
        sync = next(s for s in result if s.name == "pheasant.sync")
        assert sync.span_id.hex() == recorded[0]["span_id"]
        assert next(s for s in result if s.name == "grpc.post").parent_span_id == sync.span_id
        assert logs and "private-token" not in str(logs) and "private-key" not in str(logs)
        assert "pheasant-api" in str(traces)
    finally:
        tracing.shutdown()


def test_sampling_zero_and_remote_unsampled_do_not_export(monkeypatch, collector):
    from pheasant.telemetry import interactions
    from pheasant.telemetry.interactions import InteractionContext, observe, traceparent_header

    endpoint, protocol, traces, _ = collector
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol)
    tracing = Tracing()
    monkeypatch.setattr(interactions, "TRACING", tracing)
    try:
        tracing.configure(ObservabilitySettings(sample_ratio=0))
        with observe(None, InteractionContext.create(), kb_id="test", operation="root"):
            assert traceparent_header().endswith("-00")
        tracing.configure(ObservabilitySettings(sample_ratio=1))
        with observe(
            None,
            InteractionContext.create(traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-00"),
            kb_id="test",
            operation="remote",
        ):
            assert traceparent_header().endswith("-00")
        tracing.flush()
        assert not flattened(traces)
    finally:
        tracing.shutdown()


def test_ui_payload_rejects_content_and_zero_identifiers():
    from pydantic import ValidationError

    from pheasant.api.telemetry_routes import UIBatch

    with pytest.raises(ValidationError):
        UIBatch.model_validate({"events": [{"query_text": "never collect"}]})
    with pytest.raises(ValidationError):
        UIBatch.model_validate({"events": [{}] * 51})


@pytest.mark.parametrize("enabled", [False, True])
def test_http_tracing_independent_of_ledger_and_ui_ingestion(
    monkeypatch, tmp_path, collector, enabled
):
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from pheasant.config.schema import PheasantConfig
    from pheasant.telemetry import interactions

    endpoint, protocol, traces, _ = collector
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol)
    monkeypatch.setenv("READER_TELEMETRY_TOKEN", "reader-test-token")
    tracing = Tracing()
    monkeypatch.setattr(interactions, "TRACING", tracing)
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "defaults": "standalone",
                "name": "otel-test",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "observability": {"ui_events": enabled, "interactions": {"enabled": False}},
            "security": {
                "api_auth": {"enabled": True, "role_tokens": {"READER_TELEMETRY_TOKEN": "Reader"}}
            },
        }
    )
    app = create_app(config)
    try:
        with TestClient(app, base_url="http://localhost:8765") as client:
            assert app.state.interaction_buffer is None
            auth = {"Authorization": "Bearer reader-test-token"}
            response = client.get("/sources", headers=auth)
            assert response.status_code == 200
            assert client.post("/telemetry/ui", json={"events": []}).status_code == 401
            event = {
                "trace_id": "a" * 32,
                "span_id": "b" * 16,
                "action": "ask",
                "route": "/",
                "started_at": datetime.now(UTC).isoformat(),
                "duration_ms": 1,
            }
            response = client.post("/telemetry/ui", json={"events": [event]}, headers=auth)
            assert response.status_code == (204 if enabled else 404), response.text
            if enabled:
                assert (
                    client.post(
                        "/telemetry/ui",
                        json={"events": [{**event, "query": "secret"}]},
                        headers=auth,
                    ).status_code
                    == 422
                )
                assert (
                    client.post(
                        "/telemetry/ui",
                        json={"events": [{**event, "trace_id": "0" * 32}]},
                        headers=auth,
                    ).status_code
                    == 422
                )
            mcp_trace = "c" * 32
            response = client.post(
                "/mcp/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "list_sources",
                        "arguments": {"knowledge_base": "otel-test"},
                    },
                },
                headers={
                    **auth,
                    "accept": "application/json, text/event-stream",
                    "traceparent": f"00-{mcp_trace}-{'d' * 16}-01",
                },
            )
            assert response.status_code == 200, response.text
            tracing.flush()
            spans = flattened(traces)
            assert any(s.name == "list_sources" and s.trace_id.hex() == mcp_trace for s in spans)

            assert any(s.name == "/sources" and s.kind == 2 for s in spans)
            assert (
                any(
                    s.name == "ui.ask"
                    and s.trace_id.hex() == "a" * 32
                    and s.span_id.hex() == "b" * 16
                    for s in spans
                )
                == enabled
            )
    finally:
        tracing.shutdown()
        app.state.engine.close()


def test_real_nats_postgres_and_grpc_join_one_trace(monkeypatch, collector, tmp_path):
    import os
    import socket
    import uuid

    dsn = os.environ.get("PHEASANT_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set PHEASANT_TEST_POSTGRES_DSN and run NATS on port 14222")
    try:
        with socket.create_connection(("127.0.0.1", 14222), timeout=2):
            pass
    except OSError:
        pytest.skip("no isolated NATS broker on port 14222")
    import grpc

    from pheasant.persistence.backends import PostgresBackend
    from pheasant.sync.grpc_worker import GrpcTransport, PreparationWorkerServicer, load_protos
    from pheasant.sync.queue import IndexTask, NatsQueue
    from pheasant.sync.remote_worker import ResultCache
    from pheasant.telemetry import interactions
    from pheasant.telemetry.interactions import InteractionContext, adopt_trace, observe
    from pheasant.telemetry.spans import span
    from tests.test_grpc_worker import _config, _task, _write_docs

    endpoint, protocol, traces, _ = collector
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", protocol)
    tracing = Tracing()
    monkeypatch.setattr(interactions, "TRACING", tracing)
    tracing.configure(ObservabilitySettings())
    suffix = uuid.uuid4().hex[:12]
    queue = NatsQueue(
        ["nats://127.0.0.1:14222"],
        stream="OTEL_" + suffix,
        subject="otel." + suffix,
        durable="otel-" + suffix,
    )
    backend = PostgresBackend(dsn)
    workspace = tmp_path / "worker"
    _write_docs(workspace, 1)
    config = _config(tmp_path, workspace)
    config.sync.concurrency.remote_worker_enabled = True
    monkeypatch.setenv("PHEASANT_INDEX_WORKER_TOKEN", "grpc-token")
    _, rpc = load_protos()
    server = grpc.server(ThreadPoolExecutor(max_workers=2))
    rpc.add_PreparationWorkerServicer_to_server(
        PreparationWorkerServicer(config, ResultCache(maxsize=8)), server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    transport = GrpcTransport()
    try:
        with observe(None, InteractionContext.create(), kb_id="test", operation="/sync") as event:
            queue.publish(IndexTask(id=suffix, source_id="test", mode="incremental", payload={}))
        task = queue.claim("validation")
        assert task is not None
        with adopt_trace(task.payload.get("traceparent")):
            with span("pheasant.indexer.task", kind="CONSUMER"):
                assert backend.rows("SELECT 1 AS value")[0]["value"] == 1
                result, _ = transport.post(
                    None,
                    f"grpc://127.0.0.1:{port}",
                    [_task(tmp_path)],
                    ["test"],
                    token="grpc-token",
                    timeout=30,
                    deadline_seconds=None,
                )
                assert result["results"][0]["parsed"] is not None
        queue.ack(task)
        tracing.flush()
        result = flattened(traces)
        named = {s.name: s for s in result}
        for name in [
            "nats.publish",
            "pheasant.indexer.task",
            "postgresql.rows_once",
            "grpc.post",
            "grpc.PrepareBatch",
        ]:
            assert name in named
            assert named[name].trace_id.hex() == event.trace_id
        assert named["pheasant.indexer.task"].parent_span_id == named["nats.publish"].span_id
        assert named["grpc.PrepareBatch"].parent_span_id == named["grpc.post"].span_id
        assert "SELECT 1" not in str(traces) and "grpc-token" not in str(traces)
    finally:
        queue.close()
        backend.close()
        server.stop(0).wait()
        tracing.shutdown()
