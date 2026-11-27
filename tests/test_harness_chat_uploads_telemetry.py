"""``@harness`` in the chat, uploads read as the index reads them, and telemetry.

Three things a person meets outside the Harness page:

* the chat's ``@harness`` keyword starts a run from what they have open, through
  the extension contract (the core imports no extension), with no model call,
  idempotently, and only within what they could already read;
* a file attached to a run is read by Pheasant's own ingestion (archives
  expanded, images captioned, audio transcribed, notebooks and HTML extracted),
  and the metadata the index gives it travels into the uploads receipt and back;
* every finished run becomes one trace, emitted once, counted in Prometheus,
  summarized for the Admin page, and every export destination can be tested --
  here against a real local OTLP collector, because no cloud is connected.
"""
# ruff: noqa: E501

from __future__ import annotations

import dataclasses
import io
import json
import math
import struct
import threading
import wave
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from tests.harness_support import build_region, write_tree

pytest.importorskip("pheasant_harness")

from pheasant_harness import attachments, service, telemetry  # noqa: E402
from pheasant_harness.context import harness_for  # noqa: E402

from pheasant.api.app import create_app  # noqa: E402
from pheasant.assistant import keywords  # noqa: E402
from pheasant.extensions import (  # noqa: E402
    ChatCommand,
    Extension,
    extension_chat_commands,
    installed_extensions,
)
from pheasant.ingestion import pipeline  # noqa: E402
from pheasant.security.identity import Identity, bind_identity, reset_identity  # noqa: E402

SOURCES = (
    "\n  - name: notes\n    type: markdown_folder\n"
    '    path: "{workspace}/notes"\n    include: ["**/*.md"]\n'
)
NOTES = {
    "oncall/handbook.md": "# On-call handbook\n\nThe on-call manager approves every rollback.\nPage the platform team for database incidents.\n",
    "oncall/escalation.md": "# Escalation\n\nEscalate to the incident commander after 30 minutes.\n",
    "billing/refunds.md": "# Refunds\n\nRefunds within 30 days are approved automatically.\n",
}


@pytest.fixture()
def region(tmp_path, monkeypatch):
    r = build_region(tmp_path, monkeypatch, SOURCES)
    write_tree(r.workspace / "notes", NOTES)
    r.tools.sync_source(r.config.knowledge_base_id, "notes")
    yield r


def _as(principal: str, role: str):
    return bind_identity(Identity(principal=principal, method="token", roles=(role,)))


def _chat(client, question: str, **body) -> dict:
    response = client.post("/assistant/chat", json={"question": question, **body})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# The extension contract: a chat keyword is additive, and a built-in always wins
# ---------------------------------------------------------------------------


def test_the_harness_declares_its_chat_keyword_and_the_reader_finds_it():
    assert set(extension_chat_commands()) >= {"harness", "agent"}
    read = keywords.read("@harness write a one-page brief")
    assert read.command == "harness" and read.text == "write a one-page brief"
    assert read.used == ("@harness",)
    # Only as the first word, like every other keyword.
    assert keywords.read("please @harness write it").command is None
    listed = {row["keyword"]: row for row in keywords.catalog()}
    assert listed["@harness"]["group"] == "Extensions" and listed["@harness"]["kind"] == "command"
    assert "@agent" in listed["@harness"]["aliases"]


def test_an_extension_cannot_take_over_a_built_in_keyword(monkeypatch):
    def run(services, request):  # pragma: no cover - must never be reached
        raise AssertionError("an extension answered a built-in keyword")

    rogue = Extension(
        id="rogue",
        api_version=1,
        chat_commands=(
            ChatCommand("compose", "take over @compose", "@compose x", run, aliases=("zap",)),
            ChatCommand("table", "take over @table", "@table x", run),
        ),
    )
    monkeypatch.setattr("pheasant.extensions.installed_extensions", lambda: (rogue,))
    # The built-ins answer as they always did; the rogue's own alias is still reachable.
    assert keywords.read("@compose a module").intent == "compose"
    assert keywords.read("@compose a module").command is None
    assert keywords.read("@table the regions").form == "table"
    assert (
        keywords.read("@zap now").command is None
    )  # its command name was refused, so the alias goes too
    assert "@compose" not in {r["keyword"] for r in keywords.catalog() if r["kind"] == "command"}


class _Entry:
    """An installed entry point, as `importlib.metadata` hands one over."""

    def __init__(self, name: str, extension: Extension):
        self.name, self._extension = name, extension

    def load(self):
        return lambda: self._extension


def test_extension_chat_keywords_must_be_unique_and_well_formed(monkeypatch):
    entry = _Entry

    def run(services, request):  # pragma: no cover
        return {}

    one = Extension(id="one", api_version=1, chat_commands=(ChatCommand("go", "s", "e", run),))
    two = Extension(id="two", api_version=1, chat_commands=(ChatCommand("go", "s", "e", run),))
    bad = Extension(id="bad", api_version=1, chat_commands=(ChatCommand("Go!", "s", "e", run),))
    for found, message in (
        ([entry("one", one), entry("two", two)], "Duplicate"),
        ([entry("bad", bad)], "Invalid chat keyword"),
    ):
        installed_extensions.cache_clear()
        monkeypatch.setattr("pheasant.extensions.entry_points", lambda group, found=found: found)
        with pytest.raises(RuntimeError, match=message):
            installed_extensions()
    monkeypatch.undo()
    installed_extensions.cache_clear()
    extension_chat_commands.cache_clear()
    assert "harness" in extension_chat_commands()


# ---------------------------------------------------------------------------
# @harness in the chat
# ---------------------------------------------------------------------------


def test_harness_in_the_chat_starts_one_run_from_the_open_folder(region):
    from fastapi.testclient import TestClient

    app = create_app(region.config)
    with TestClient(app) as client:
        body = _chat(
            client,
            "@harness write a one-page onboarding brief for new on-call engineers",
            current_directory="notes/oncall",
            conversation_id="conv-1",
        )
        extension = body["extension"]
        assert body["route"]["intent"] == "extension"
        assert extension["extension"] == "harness" and extension["status"] == "started"
        assert extension["workload"] == "summarize"  # no model in the suite: gathered and cited
        assert extension["destination"] == {
            "adapter": "artifact",
            "target": "write a one-page onboarding brief for new on-call engineers",
        }
        assert [i["uri"] for i in extension["inputs"]] == ["pheasant://notes/oncall"]
        assert extension["link"] == f"/harness?run={extension['run_id']}"
        assert "Open the run" in body["answer"] and "the open folder" in body["answer"]
        # Nothing was searched and no model was asked: the extension answered.
        assert body.get("citations") in (None, [])
        assert body["keywords"]["command"] == "harness"
        # The run exists, reads exactly that folder, and is the asker's.
        run = client.get(f"/harness/runs/{extension['run_id']}").json()
        assert run["state"] in ("accepted", "running", "succeeded")
        stored = region.harness.runs.get(extension["run_id"])
        assert stored["request"]["question"].startswith("write a one-page onboarding brief")
        assert stored["request"]["inputs"] == [extension["inputs"][0]["id"]]
        # The same message in the same conversation is the same run, not a second one.
        again = _chat(
            client,
            "@harness write a one-page onboarding brief for new on-call engineers",
            current_directory="notes/oncall",
            conversation_id="conv-1",
        )
        assert again["extension"]["run_id"] == extension["run_id"]
        # The alias reaches the same command.
        assert _chat(client, "@agent help")["extension"]["status"] == "help"


def test_harness_in_the_chat_uses_the_scoped_sources_then_says_what_it_needs(region):
    from fastapi.testclient import TestClient

    with TestClient(create_app(region.config)) as client:
        scoped = _chat(client, "@harness summarize the refund policy", source_names=["notes"])
        assert scoped["extension"]["workload"] == "summarize"
        assert [i["uri"] for i in scoped["extension"]["inputs"]] == ["pheasant://notes"]
        # A code change needs a repository and tests: the page, prefilled.
        change = _chat(client, "@harness fix the rollback approval check")
        assert change["extension"]["status"] == "needs_page"
        assert change["extension"]["link"].startswith("/harness?new=1&prompt=fix%20the%20rollback")
        assert "run_id" not in change["extension"]
        # Bare keyword: how to use it, nothing started.
        help_answer = _chat(client, "@harness")
        assert (
            help_answer["extension"]["status"] == "help"
            and "@harness write" in help_answer["answer"]
        )


def test_quoted_phrases_are_what_a_chat_run_looks_for_and_it_cites_them(region):
    """Without a model the rules planner finds only what it is told to; quotes tell it."""
    from pheasant_harness.chat import run_command

    from pheasant.extensions import ChatCommandRequest

    plain = run_command(
        region.services,
        ChatCommandRequest(text="write a rollback brief", keyword="harness", source_name="notes"),
    )
    assert plain["identifiers"] == [] and "put exact phrases in quotes" in plain["text"]
    started = run_command(
        region.services,
        ChatCommandRequest(
            text='write a rollback brief covering "approves every rollback" and “incident commander”',
            keyword="harness",
            source_name="notes",
        ),
    )
    assert started["identifiers"] == ["approves every rollback", "incident commander"]
    assert (
        "“approves every rollback”" in started["text"]
        and "quotes" not in started["text"].split("exactly.")[1]
    )
    final = region.coordinator().drive(started["run_id"])
    # No model here: a "write" request is gathered and cited rather than written, and says so.
    assert started["workload"] == "summarize" and "gathers and cites" in started["text"]
    assert final["state"] == "succeeded", final["result"]
    brief = region.text(final["result"]["candidates"]["primary"])
    assert "on-call manager approves every rollback" in brief


def test_harness_in_the_chat_with_nothing_to_read_starts_nothing(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    r = build_region(tmp_path, monkeypatch)
    with TestClient(create_app(r.config)) as client:
        body = _chat(client, "@harness write a brief about deployments")
        assert "extension" not in body
        assert "did not start" in body["answer"] and "nothing for it to read" in body["answer"]
        assert client.get("/harness/runs").json()["runs"] == []


def test_harness_in_the_chat_never_widens_what_a_reader_can_read(region, monkeypatch):
    """With document ACLs on, a Reader cannot register a source by chatting."""
    services = region.services
    monkeypatch.setattr(region.config.security, "acl_enforced", True)
    from pheasant_harness.chat import run_command

    from pheasant.extensions import ChatCommandRequest

    token = _as("reader", "Reader")
    try:
        with pytest.raises(ValueError, match="not registered for you yet"):
            run_command(
                services,
                ChatCommandRequest(text="write a brief", keyword="harness", source_name="notes"),
            )
        assert harness_for(services).registry.list("reader") == []
    finally:
        reset_identity(token)
    # Once someone who manages sources registers and grants it, the reader may use it.
    reg = harness_for(services).registry.register("pheasant://notes", "ops")
    harness_for(services).registry.grant(reg["id"], "reader", "ops")
    token = _as("reader", "Reader")
    try:
        started = run_command(
            services,
            ChatCommandRequest(text="write a brief", keyword="harness", source_name="notes"),
        )
        assert started["status"] == "started" and started["inputs"][0]["id"] == reg["id"]
    finally:
        reset_identity(token)


def test_harness_in_the_chat_agrees_over_mcp(region):
    """`ask_knowledge_base` reaches the same command and the same run."""
    from pheasant.services import assistant

    kb = region.config.knowledge_base_id
    payload = region.tools.ask_knowledge_base(
        kb,
        "@harness write an escalation cheat sheet",
        source_names=["notes"],
        conversation_id="conv-9",
    )
    direct = assistant.answer(
        region.services,
        assistant.AnswerRequest(
            question="@harness write an escalation cheat sheet",
            source_names=["notes"],
            conversation_id="conv-9",
        ),
    )
    assert payload["extension"]["run_id"] == direct["extension"]["run_id"]
    assert payload["route"]["intent"] == "extension"


# ---------------------------------------------------------------------------
# Uploads: every input type, read by Pheasant's ingestion, metadata mapped
# ---------------------------------------------------------------------------

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f"
    b"\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)
NOTEBOOK = json.dumps(
    {
        "cells": [
            {"cell_type": "markdown", "source": ["# Capacity model\n", "Peak load is 4,000 rps."]},
            {"cell_type": "code", "source": ["rate = 4000\n"], "outputs": []},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
).encode()
HTML = b"<html><head><title>Runbook</title><script>var x=1;</script></head><body><h1>Runbook</h1><p>Restart with make restart-api.</p></body></html>"


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8000)
        out.writeframes(b"".join(struct.pack("<h", int(800 * math.sin(i / 8))) for i in range(800)))
    return buffer.getvalue()


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _files() -> list[tuple[str, bytes]]:
    inner = _zip({"deep/note.md": b"# Deep note\n\nNested archives are expanded too.\n"})
    return [
        ("intake/screen.png", PNG),
        ("intake/standup.wav", _wav()),
        ("intake/capacity.ipynb", NOTEBOOK),
        ("intake/runbook.html", HTML),
        ("intake/notes.md", b"# Notes\n\nPlain text stays as it is.\n"),
        (
            "bundle.zip",
            _zip(
                {
                    "docs/policy.md": b"# Policy\n\nArchived policies are expanded.\n",
                    "docs/diagram.png": PNG,
                    "inner.zip": inner,
                    "tools/blob.bin": b"\x00\x01binary",
                }
            ),
        ),
    ]


def _no_sync(services):
    original = services.indexing
    object.__setattr__(
        services, "indexing", dataclasses.replace(original, start_sync=lambda name: {})
    )
    return original


def test_every_input_type_is_read_the_way_the_index_reads_it(region):
    harness = region.harness
    services = region.services
    original = _no_sync(services)
    try:
        attached = attachments.attach(harness, _files(), "local", label="Intake")
    finally:
        object.__setattr__(services, "indexing", original)
    files = {f["path"]: f for f in attached["files"]}
    readers = attachments._Readers(harness)
    for path, how, twin in (
        ("intake/screen.png", "caption", "intake/screen.png.caption.txt"),
        ("intake/standup.wav", "transcript", "intake/standup.wav.transcript.txt"),
        ("intake/capacity.ipynb", "notebook", "intake/capacity.ipynb.txt"),
        ("intake/runbook.html", "html", "intake/runbook.html.txt"),
    ):
        assert files[path]["text_from"] == how, path
        assert files[twin]["derived_from"] == path and files[twin]["text_from"] == how
        # The twin is exactly what the index reads from those bytes: the same code.
        data = dict(_files())[path]
        expected, read_by = pipeline.payload_text(
            path,
            data,
            files[path]["mime_type"],
            captioner=readers.captioner,
            transcriber=readers.transcriber,
            extractor=readers.extractor,
        )
        assert read_by == how
        registry = harness.registry
        revision = registry.resolve(attached["registration"]["id"], "local")
        resolver = registry.resolver(registry.get(attached["registration"]["id"]))
        assert resolver.read_bytes(revision, twin).decode() == str(expected)
    page = resolver.read_bytes(revision, "intake/runbook.html.txt").decode()
    assert "Restart with make restart-api" in page and "var x" not in page
    # Plain text gets no twin; metadata says what each file is.
    assert files["intake/notes.md"]["text_from"] == "text" and "intake/notes.md.txt" not in files
    assert files["intake/screen.png"]["mime_type"] == "image/png"
    assert files["intake/screen.png"]["artifact_type"] == "image"
    assert files["intake/standup.wav"]["artifact_type"] == "audio"
    # The archive is kept and expanded, nested archives included; what the index skips is counted.
    assert (
        files["bundle.zip"]["text_from"] == "archive" and files["bundle.zip"]["indexable"] is True
    )
    assert files["bundle/docs/policy.md"]["archive"] == "bundle.zip"
    assert files["bundle/docs/diagram.png.caption.txt"]["derived_from"] == "bundle/docs/diagram.png"
    assert files["bundle/inner.zip/deep/note.md"]["archive"] == "bundle.zip"
    assert "bundle/tools/blob.bin" not in files
    summary = attached["archives"]["bundle.zip"]
    assert summary["folder"] == "bundle" and summary["nested_archives"] == 1
    assert summary["skipped"] == {".bin": 1}


def test_attached_files_carry_their_metadata_into_uploads_and_back(region):
    harness = region.harness
    services = region.services
    original = _no_sync(services)
    try:
        attached = attachments.attach(harness, _files(), "local", label="Intake")
    finally:
        object.__setattr__(services, "indexing", original)
    batch, reg = attached["batch"], attached["registration"]["id"]
    assert attached["uploads"]["landed"] == 6  # originals only: the archive lands whole
    receipts = {
        row["idempotency_key"]: json.loads(row["detail_json"])
        for row in services.state.rows(
            "SELECT idempotency_key, detail_json FROM ingest_receipts WHERE submission_id=?",
            (f"harness-{batch}",),
        )
    }
    detail = receipts[f"harness-attachment:{batch}:intake/screen.png"]
    assert detail["harness_registration"] == reg and detail["harness_batch"] == batch
    assert detail["mime_type"] == "image/png" and detail["artifact_type"] == "image"
    assert detail["text_from"] == "caption" and detail["uploaded_by"] == "local"
    assert detail["attachment_label"] == "Intake" and detail["original_name"] == "intake/screen.png"
    assert receipts[f"harness-attachment:{batch}:bundle.zip"]["expanded_into"] == "bundle"
    # Each original names the Pheasant document it became, and the route says the same.
    mapped = service.attachment_documents(services, reg)
    by_path = {f["path"]: f for f in mapped["files"]}
    assert by_path["intake/screen.png"]["pheasant_document"] == (
        f"file:uploads:harness/{batch}/intake/screen.png:branch=none"
    )
    assert by_path["intake/screen.png.caption.txt"]["pheasant_document"] is None
    assert (
        by_path["bundle/docs/policy.md"]["pheasant_document"] is None
    )  # inside the landed archive
    assert {f["path"]: f["pheasant_document"] for f in attached["files"]}["intake/notes.md"]
    # Indexed like any upload: the archive's members and the image's caption are searchable.
    region.tools.sync_source(region.config.knowledge_base_id, "uploads")
    hits = region.tools.search_context(
        region.config.knowledge_base_id, "Archived policies are expanded", source_name="uploads"
    )["results"]
    assert any("policy.md" in (h.get("relative_path") or "") for h in hits)


def test_attachments_refuse_what_would_not_fit(region, monkeypatch):
    from pheasant_harness.common import HarnessError

    monkeypatch.setattr(attachments, "MAX_ENTRIES", 4)
    with pytest.raises(HarnessError, match="expand to more than 4 entries"):
        attachments.build_manifest(region.harness, _files())
    broken = attachments.build_manifest(region.harness, [("bad.zip", b"not a zip at all")])[1]
    assert broken[0]["text_from"] == "archive" and broken[0]["kind"] == "file"


# ---------------------------------------------------------------------------
# Telemetry: one trace per run, metrics, the operator view, tested destinations
# ---------------------------------------------------------------------------


def _finished_run(region):
    reg = region.register("pheasant://notes")
    return region.run(
        workload="answer",
        question="Who approves a rollback?",
        inputs=[reg],
        identifiers=["approves every rollback"],
        discovery="off",
    )


def test_a_finished_run_is_one_trace_emitted_once_and_counted(region, capsys):
    status = _finished_run(region)
    assert status["state"] == "succeeded", status["result"]
    capsys.readouterr()
    assert telemetry.emit_pending(region.harness) == 1
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith('{"type":"otel.span"')
    ]
    trace = telemetry.trace_id(status["run_id"])
    assert lines and {span["trace_id"] for span in lines} == {trace}
    root = [s for s in lines if s["parent_span_id"] is None]
    assert len(root) == 1 and root[0]["name"] == "pheasant.harness.run"
    assert root[0]["attributes"]["pheasant.harness.workload"] == "answer"
    assert "local" not in json.dumps(root[0]["attributes"])  # never the person who asked
    names = {s["name"] for s in lines}
    assert "pheasant.harness.phase.running" in names and "pheasant.harness.verify" in names
    assert any(n.startswith("pheasant.harness.step.") for n in names)
    assert all(s["parent_span_id"] == root[0]["span_id"] for s in lines if s is not root[0])
    # Emitted once: a second pass (another replica's runner, say) finds it claimed.
    assert telemetry.emit_pending(region.harness) == 0
    assert capsys.readouterr().out == ""
    # Even when that replica selected the run before this one's claim committed:
    # the claim, not the selection, is what keeps it to one emission.
    state = region.harness.state
    real_rows = state.rows

    def stale(sql, params=()):
        if "LEFT JOIN harness_telemetry_traces" in sql:
            return [{"id": status["run_id"]}]
        return real_rows(sql, params)

    state.rows = stale
    try:
        assert telemetry.emit_pending(region.harness) == 0
    finally:
        del state.rows
    assert capsys.readouterr().out == ""
    from pheasant.telemetry.metrics import REGISTRY

    assert REGISTRY.value("pheasant_harness_runs_total", workload="answer", state="succeeded") >= 1
    assert REGISTRY.value("pheasant_harness_traces_emitted_total", exported="false") >= 1
    rendered = REGISTRY.render()
    assert (
        "pheasant_harness_run_seconds_bucket" in rendered
        and "pheasant_harness_runs_live" in rendered
    )
    # The span-by-span view is the same trace the log holds.
    view = service.run_trace(region.services, status["run_id"])
    assert view["trace_id"] == trace and len(view["spans"]) == len(lines)


def test_the_operator_overview_counts_what_the_tables_hold(region):
    status = _finished_run(region)
    overview = service.telemetry(region.services, 24)
    assert overview["runs"]["total"] == 1 and overview["runs"]["by_state"] == {"succeeded": 1}
    assert overview["runs"]["success_rate"] == 1.0 and overview["runs"]["p50_seconds"] is not None
    assert overview["workloads"]["answer"]["succeeded"] == 1
    # Spend is recorded per step attempt; the overview, the row and the span all sum it.
    attempts = region.services.state.rows(
        "SELECT spent_json FROM harness_attempts WHERE run_id=?", (status["run_id"],)
    )
    tools = sum(json.loads(a["spent_json"] or "{}").get("tool_calls", 0) for a in attempts)
    assert tools > 0 and overview["usage"]["tool_calls"] == tools
    assert overview["recent_runs"][0]["usage"]["tool_calls"] == tools
    root = telemetry.run_spans(region.harness, region.harness.runs.get(status["run_id"]))[0]
    assert root["attributes"]["pheasant.harness.usage.tool_calls"] == tools
    assert overview["verifications"].get("pass", 0) >= 1
    assert overview["traces"]["emitted"] == 1
    # Numbers are JSON numbers: PostgreSQL's AVG is a decimal, which crashed the Admin tab.
    assert overview["steps"] and all(
        isinstance(s["mean_seconds"], float) and isinstance(s["n"], int) for s in overview["steps"]
    )
    json.dumps(overview)
    recent = overview["recent_runs"][0]
    assert recent["run_id"] == status["run_id"] and recent["trace"] == "emitted"
    assert recent["trace_id"] == telemetry.trace_id(status["run_id"])
    assert {d["id"] for d in overview["destinations"]} == {
        "prometheus",
        "stdout",
        "otlp",
        "azure_monitor",
    }
    otlp = next(d for d in overview["destinations"] if d["id"] == "otlp")
    assert otlp["configured"] is False and "otlp_endpoint" in otlp["detail"]
    assert overview["metrics"]["pheasant_harness_runs_total"]
    with pytest.raises(Exception, match="window_hours"):
        service.telemetry(region.services, 0)


def test_telemetry_is_an_operators_view(region):
    token = _as("reader", "Reader")
    try:
        with pytest.raises(Exception, match="security.manage"):
            service.telemetry(region.services)
        with pytest.raises(Exception, match="security.manage"):
            service.test_telemetry(region.services, "stdout")
    finally:
        reset_identity(token)


class _Collector(BaseHTTPRequestHandler):
    received: list[tuple[str, str, bytes]] = []

    def do_POST(self):  # noqa: N802 - the stdlib's spelling
        length = int(self.headers.get("Content-Length") or 0)
        type(self).received.append(
            (self.path, self.headers.get("Content-Type", ""), self.rfile.read(length))
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):  # quiet
        pass


@pytest.fixture()
def collector():
    _Collector.received = []
    server = HTTPServer(("127.0.0.1", 0), _Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1/traces", _Collector.received
    server.shutdown()


def test_destinations_without_a_cloud_say_so_and_local_ones_are_proven(region, capsys, monkeypatch):
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    otlp = service.test_telemetry(region.services, "otlp")
    assert otlp["ok"] is False and otlp["outcome"] == "not_configured"
    azure = service.test_telemetry(region.services, "azure_monitor")
    assert azure["ok"] is False and azure["outcome"] == "not_configured"
    stdout = service.test_telemetry(region.services, "stdout")
    assert stdout["ok"] is True
    assert stdout["trace_id"] in capsys.readouterr().out
    assert service.test_telemetry(region.services, "prometheus")["ok"] is True
    with pytest.raises(Exception, match="unknown telemetry destination"):
        service.test_telemetry(region.services, "carrier-pigeon")
    # Each test is recorded with who ran it, and shown beside its destination.
    rows = {d["id"]: d for d in telemetry.destinations(region.harness)}
    assert rows["otlp"]["last_test"]["outcome"] == "not_configured"
    assert (
        rows["stdout"]["last_test"]["ok"] is True
        and rows["stdout"]["last_test"]["actor"] == "local"
    )


def test_an_otlp_collector_receives_the_test_span_through_the_sdk(region, collector, monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    endpoint, received = collector
    monkeypatch.setattr(region.config.observability, "otlp_endpoint", endpoint)
    result = service.test_telemetry(region.services, "otlp")
    assert result["ok"] is True and result["via"].startswith("OpenTelemetry SDK")
    assert received and received[0][0] == "/v1/traces"
    assert received[0][1] == "application/x-protobuf"
    assert bytes.fromhex(result["trace_id"]) in received[0][2]
    # Azure Monitor is reached through that collector once it has a connection string.
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=https://westus2-1.in.applicationinsights.azure.com/",
    )
    rows = {d["id"]: d for d in telemetry.destinations(region.harness)}
    assert rows["azure_monitor"]["configured"] is True
    assert rows["azure_monitor"]["where"] == "https://westus2-1.in.applicationinsights.azure.com/"
    assert service.test_telemetry(region.services, "azure_monitor")["ok"] is True
    assert len(received) == 2


def test_an_otlp_collector_receives_json_when_the_sdk_is_absent(region, collector, monkeypatch):
    import sys

    endpoint, received = collector
    monkeypatch.setattr(region.config.observability, "otlp_endpoint", endpoint)
    monkeypatch.setitem(sys.modules, "opentelemetry.sdk.trace.export", None)
    result = service.test_telemetry(region.services, "otlp")
    assert result["ok"] is True and result["via"] == "OTLP/JSON" and result["status_code"] == 200
    path, content_type, body = received[0]
    assert path == "/v1/traces" and content_type == "application/json"
    spans = json.loads(body)["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert (
        spans[0]["traceId"] == result["trace_id"]
        and spans[0]["name"] == "pheasant.harness.telemetry_test"
    )


def test_an_unreachable_collector_is_reported_not_raised(region, monkeypatch):
    monkeypatch.setattr(
        region.config.observability, "otlp_endpoint", "http://127.0.0.1:9/v1/traces"
    )
    result = service.test_telemetry(region.services, "otlp")
    assert result["ok"] is False and result["outcome"] in ("unreachable", "refused")


def test_telemetry_agrees_over_http_and_mcp(region):
    """The Admin tab's data and the agent's are one operation."""
    import asyncio

    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    _finished_run(region)
    kb = region.config.knowledge_base_id
    app = create_app(region.config)

    async def scenario():
        base = "http://localhost:8765"
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url=base
            ) as http:
                over_http = (
                    await http.get("/harness/telemetry", params={"window_hours": 24})
                ).json()
                async with Client(
                    streamable_http_client(f"{base}/mcp/", http_client=http)
                ) as client:
                    result = await client.call_tool(
                        "harness_telemetry", {"knowledge_base": kb, "window_hours": 24}
                    )
                    over_mcp = result.structured_content or json.loads(result.content[0].text)
                    tested = await client.call_tool(
                        "harness_test_telemetry", {"knowledge_base": kb, "destination": "otlp"}
                    )
                    tested_mcp = tested.structured_content or json.loads(tested.content[0].text)
                tested_http = (
                    await http.post("/harness/telemetry/test", json={"destination": "otlp"})
                ).json()
        return over_http, over_mcp, tested_http, tested_mcp

    over_http, over_mcp, tested_http, tested_mcp = asyncio.run(scenario())
    for key in ("runs", "workloads", "verifications", "traces", "usage"):
        assert over_http[key] == over_mcp[key], key
    assert tested_http["outcome"] == tested_mcp["outcome"] == "not_configured"
