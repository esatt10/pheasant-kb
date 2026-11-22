"""Acceptance tests for PDF/DOCX/HTML document text extraction.

Fully offline. The gap these close, stated as a behavior: ``.pdf`` and
``.docx`` were **accepted** by the ingestion pipeline and then produced no
text — the artifact was discovered, hashed, typed ``"document"`` and given a
graph node, while ``read_text`` returned ``""`` so it contributed zero chunks
and could not be found by its content. ``test_pdf_used_to_index_no_text_at_all``
pins the old behavior explicitly so the regression is visible if the wiring is
ever removed.

Fixtures are hand-authored and byte-deterministic (``tests/fixtures/
sample_workspace/documents/``): ``handbook.pdf`` carries page 1 as an
**uncompressed** stream and page 2 as a **FlateDecode** stream, so one file
exercises both carving paths; ``report.docx`` is a fixed-timestamp ZIP;
``scanned.pdf`` has no text operators at all and exists to prove the authored
``.extract.txt`` sidecar path.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import ExtractorSettings, PheasantConfig
from pheasant.ingestion.captioner import source_includes_images
from pheasant.ingestion.extractor import (
    AutoExtractor,
    BuiltinExtractor,
    NativeExtractor,
    build_extractor,
    config_has_document_source,
    extract_docx_text_builtin,
    extract_html_text,
    extract_pdf_text_builtin,
    extractor_from_config,
    iter_pdf_content_streams,
    scan_pdf_content_stream,
    source_includes_documents,
)
from pheasant.ingestion.pipeline import parse_file, read_text, read_text_bytes
from pheasant.ingestion.transcriber import source_includes_audio
from pheasant.search.hybrid import HybridSearch
from pheasant.search.text_store import SearchStore
from pheasant.sync.engine import SyncEngine

DOCS = Path(__file__).parent / "fixtures" / "sample_workspace" / "documents"
HANDBOOK = DOCS / "handbook.pdf"
REPORT = DOCS / "report.docx"
SCANNED = DOCS / "scanned.pdf"
SCANNED_SIDECAR = DOCS / "scanned.pdf.extract.txt"


def test_text_nul_is_removed_before_chunk_persistence(tmp_path: Path) -> None:
    document = tmp_path / "binary-fixture.txt"
    document.write_bytes(b"before\x00after")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "nul-safe",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [
                {
                    "name": "fixtures",
                    "type": "document_folder",
                    "path": str(tmp_path),
                    "include": ["**/*.txt"],
                }
            ],
        }
    )

    parsed = parse_file(config.sources[0], document, git_metadata=(None, None, False))

    assert parsed is not None
    assert "".join(chunk.text for chunk in parsed.chunks) == "beforeafter"


def _make_document_engine(tmp_path: Path, *, provider: str = "auto") -> SyncEngine:
    docs = tmp_path / "workspace" / "documents"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "handbook.pdf").write_bytes(HANDBOOK.read_bytes())
    (docs / "report.docx").write_bytes(REPORT.read_bytes())
    (docs / "scanned.pdf").write_bytes(SCANNED.read_bytes())
    (docs / "scanned.pdf.extract.txt").write_bytes(SCANNED_SIDECAR.read_bytes())

    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "document-acceptance",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "ingestion": {"extractor": {"provider": provider}},
            "sources": [
                {
                    "name": "documents",
                    "type": "document_folder",
                    "path": str(docs),
                    "include": ["**/*.pdf", "**/*.docx", "**/*.md"],
                }
            ],
        }
    )
    return SyncEngine(config)


def _search(engine: SyncEngine, query: str, mode: str = "text") -> list[str]:
    searcher = HybridSearch(SearchStore(engine.state), vector=None)
    payload = searcher.search_context(
        engine.config.knowledge_base_id, query, mode=mode, max_results=10
    )
    return [str(item.get("relative_path") or "") for item in payload["results"]]


# ---------------------------------------------------------------------------
# The gap, pinned
# ---------------------------------------------------------------------------


def test_pdf_used_to_index_no_text_at_all() -> None:
    """Without an extractor, a text-bearing PDF still yields nothing.

    This is the pre-existing behavior and the reason the feature was needed;
    it is also the "unchanged when unused" guarantee, so it must keep holding
    for a region that configures no document source.
    """
    assert read_text(HANDBOOK) == ""
    assert read_text_bytes(HANDBOOK.read_bytes(), "handbook.pdf") == ""


def test_pdf_and_docx_yield_text_once_an_extractor_is_supplied() -> None:
    extractor = AutoExtractor()
    pdf_text = read_text(HANDBOOK, extractor)
    assert "ZEBRAFISH" in pdf_text  # page 1: uncompressed stream
    assert "MARMOSET" in pdf_text  # page 2: FlateDecode stream

    docx_text = read_text_bytes(REPORT.read_bytes(), "report.docx", extractor)
    assert "OKAPI" in docx_text


def test_stateless_worker_prepares_pdf_with_forwarded_offline_extractor_settings(
    tmp_path: Path,
) -> None:
    """The fleet path must actually handle documents, not fall back locally."""

    from pheasant.sync.connectors import ConnectorItem, ConnectorPayload
    from pheasant.sync.remote_worker import prepare_task, task_payload

    config = PheasantConfig.model_validate(
        {
            "ingestion": {"extractor": {"provider": "builtin"}},
            "sources": [
                {
                    "name": "documents",
                    "type": "document_folder",
                    "path": str(tmp_path),
                    "include": ["**/*.pdf"],
                }
            ],
        }
    )
    content = HANDBOOK.read_bytes()
    item = ConnectorItem(
        identity="handbook",
        relative_path="handbook.pdf",
        uri=(tmp_path / "handbook.pdf").as_uri(),
        size_bytes=len(content),
    )
    payload = ConnectorPayload(item=item, content=content, size_bytes=len(content))
    wire = prepare_task(
        task_payload(
            config.sources[0],
            item,
            payload,
            None,
            config.ingestion.extractor,
        )
    )

    assert wire is not None
    assert wire["relative_path"] == "handbook.pdf"
    assert any("ZEBRAFISH" in chunk["text"] for chunk in wire["chunks"])


# ---------------------------------------------------------------------------
# Builtin (pure stdlib) extraction
# ---------------------------------------------------------------------------


def test_builtin_pdf_reads_both_uncompressed_and_flate_streams() -> None:
    text = extract_pdf_text_builtin(HANDBOOK.read_bytes())
    assert "Pheasant Handbook: Retrieval Architecture" in text
    assert "ZEBRAFISH" in text
    assert "Chapter Two: Federation" in text
    assert "MARMOSET" in text


def test_builtin_pdf_resolves_string_escapes() -> None:
    text = extract_pdf_text_builtin(HANDBOOK.read_bytes())
    # The fixture writes `\(parens\)` and a literal backslash; both must
    # survive as their unescaped selves rather than leaking PDF syntax.
    assert "(parens)" in text
    assert "backslash \\ here" in text


def test_builtin_docx_reads_paragraphs_and_tables() -> None:
    text = extract_docx_text_builtin(REPORT.read_bytes())
    assert "Quarterly Report: Ingestion" in text
    assert "OKAPI" in text
    assert "PDF and DOCX" in text  # table cell


def test_builtin_matches_native_on_docx() -> None:
    """DOCX is not a fidelity compromise — both paths read the same XML."""
    assert extract_docx_text_builtin(REPORT.read_bytes()) == NativeExtractor().extract(
        REPORT.read_bytes(), "report.docx"
    )


def test_html_extraction_drops_script_and_style() -> None:
    html = (
        b"<html><head><style>p{color:red}</style></head>"
        b"<body><h1>Title</h1><script>steal()</script><p>Real prose</p></body></html>"
    )
    text = extract_html_text(html)
    assert "Title" in text and "Real prose" in text
    assert "steal" not in text and "color:red" not in text


def test_malformed_documents_return_empty_rather_than_raising() -> None:
    extractor = AutoExtractor()
    for payload, name in [
        (b"", "empty.pdf"),
        (b"not a pdf at all", "bogus.pdf"),
        (b"%PDF-1.4\ntruncated", "truncated.pdf"),
        (b"PK\x03\x04 not really a zip", "bogus.docx"),
    ]:
        assert extractor.extract(payload, name) == ""


def test_tidy_is_idempotent() -> None:
    """Stable text is what makes re-syncing an unchanged document idempotent."""
    from pheasant.ingestion.extractor import _tidy

    once = _tidy("a  b\r\n\r\n\r\n\r\nc \n d\t\te  ")
    assert _tidy(once) == once


# ---------------------------------------------------------------------------
# Sidecar + provider selection
# ---------------------------------------------------------------------------


def test_authored_sidecar_wins_over_every_provider() -> None:
    sidecar = SCANNED_SIDECAR.read_bytes()
    content = SCANNED.read_bytes()
    assert extract_pdf_text_builtin(content) == ""  # nothing to extract
    for extractor in (BuiltinExtractor(), NativeExtractor(), AutoExtractor()):
        assert "PANGOLIN" in extractor.extract(content, "scanned.pdf", sidecar=sidecar)


def test_build_extractor_provider_selection() -> None:
    assert isinstance(build_extractor(ExtractorSettings(provider="auto")), AutoExtractor)
    assert isinstance(build_extractor(ExtractorSettings(provider="builtin")), BuiltinExtractor)
    assert isinstance(build_extractor(ExtractorSettings(provider="native")), NativeExtractor)
    with pytest.raises(ValueError, match="Unsupported"):
        build_extractor(ExtractorSettings(provider="bogus"))


def test_native_falls_back_to_builtin_when_pdfium_is_missing(monkeypatch: Any) -> None:
    """A missing optional wheel must degrade that format, not fail the sync."""
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "pypdfium2" or name.startswith("pypdfium2."):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    text = NativeExtractor().extract(HANDBOOK.read_bytes(), "handbook.pdf")
    assert "ZEBRAFISH" in text  # recovered by the pure-stdlib path


def test_auto_prefers_native_but_recovers_when_it_yields_nothing(monkeypatch: Any) -> None:
    extractor = AutoExtractor()
    monkeypatch.setattr(extractor._native, "extract", lambda *a, **k: "")
    assert "ZEBRAFISH" in extractor.extract(HANDBOOK.read_bytes(), "handbook.pdf")


# ---------------------------------------------------------------------------
# Opt-in wiring: only built when a source actually admits documents
# ---------------------------------------------------------------------------


def test_document_capability_is_opt_in_by_glob() -> None:
    class Source:
        def __init__(self, include: list[str]) -> None:
            self.include = include

    assert source_includes_documents(Source(["**/*.pdf"]))
    assert source_includes_documents(Source(["**/*.docx"]))
    assert source_includes_documents(Source(["**/*"]))
    assert source_includes_images(Source(["**/*"]))
    assert source_includes_audio(Source(["**/*"]))
    assert not source_includes_documents(Source(["**/*.py", "**/*.md"]))


def test_text_only_region_builds_no_extractor(tmp_path: Path) -> None:
    notes = tmp_path / "workspace" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "a.md").write_text("# Notes\n\nplain text only.\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "text-only",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [
                {
                    "name": "notes",
                    "type": "document_folder",
                    "path": str(notes),
                    "include": ["**/*.md"],
                }
            ],
        }
    )
    assert extractor_from_config(config) is None
    assert not config_has_document_source(config)
    assert SyncEngine(config).extractor is None


def test_extractor_config_block_is_actually_wired(tmp_path: Path) -> None:
    """Regression guard for the class of bug that hid the ``graph:`` section.

    A nested config block that the ``model_validate`` constructor forgets to
    pass through is silently discarded — the 2026-08-04 dogfooding session
    found exactly that for ``graph:``. Assert the round trip explicitly.
    """
    config = PheasantConfig.model_validate(
        {
            "pheasant": {"name": "cfg", "state_path": str(tmp_path / "state")},
            "ingestion": {"extractor": {"provider": "builtin", "html_text": True}},
        }
    )
    assert config.ingestion.extractor.provider == "builtin"
    assert config.ingestion.extractor.html_text is True


def test_html_stripping_is_off_by_default() -> None:
    """HTML has always indexed as raw markup; changing that silently would
    alter chunk boundaries for existing knowledge bases on upgrade."""
    assert ExtractorSettings().html_text is False
    from pheasant.ingestion.extractor import extractable_extensions

    assert ".html" not in extractable_extensions(ExtractorSettings())
    assert ".html" in extractable_extensions(ExtractorSettings(html_text=True))


def test_html_source_alone_builds_an_extractor_when_opted_in(tmp_path: Path) -> None:
    config = PheasantConfig.model_validate(
        {
            "pheasant": {"name": "html", "state_path": str(tmp_path / "state")},
            "ingestion": {"extractor": {"html_text": True}},
            "sources": [
                {
                    "name": "site",
                    "type": "document_folder",
                    "path": str(tmp_path),
                    "include": ["**/*.html"],
                }
            ],
        }
    )
    assert extractor_from_config(config) is not None


# ---------------------------------------------------------------------------
# Region ingest + self-search (the headline acceptance)
# ---------------------------------------------------------------------------


def test_pdf_content_is_searchable_after_sync(tmp_path: Path) -> None:
    engine = _make_document_engine(tmp_path)
    engine.sync_source("documents", mode="full")

    rows = engine.state.rows("SELECT relative_path, type FROM artifacts ORDER BY relative_path")
    by_path = {row["relative_path"]: row["type"] for row in rows}
    assert by_path.get("handbook.pdf") == "document"
    assert by_path.get("report.docx") == "document"

    # The real point: content inside the PDF is now findable, not just its path.
    assert any("handbook.pdf" in p for p in _search(engine, "ZEBRAFISH protocol resync"))
    assert any("report.docx" in p for p in _search(engine, "OKAPI extraction coverage"))
    # And the authored sidecar for an image-only scan is searchable too.
    assert any("scanned.pdf" in p for p in _search(engine, "PANGOLIN clause"))


def test_documents_produce_chunks(tmp_path: Path) -> None:
    engine = _make_document_engine(tmp_path)
    engine.sync_source("documents", mode="full")
    rows = engine.state.rows(
        "SELECT a.relative_path AS relative_path, COUNT(c.id) AS n "
        "FROM artifacts a LEFT JOIN chunks c ON c.artifact_id = a.id "
        "GROUP BY a.relative_path"
    )
    counts = {row["relative_path"]: row["n"] for row in rows}
    assert counts.get("handbook.pdf", 0) >= 1, counts
    assert counts.get("report.docx", 0) >= 1, counts


def test_resync_unchanged_documents_performs_zero_reextraction(tmp_path: Path) -> None:
    engine = _make_document_engine(tmp_path)
    engine.sync_source("documents", mode="full")
    assert engine.extractor is not None
    baseline = engine.extractor.calls
    assert baseline >= 2

    # The engine's pre-read sha256 skip never reaches the extractor for an
    # unchanged file — the same zero-work guarantee 21.4/25.4 hold.
    engine.sync_source("documents", mode="incremental")
    assert engine.extractor.calls == baseline


def test_resync_is_idempotent_for_documents(tmp_path: Path) -> None:
    engine = _make_document_engine(tmp_path)
    engine.sync_source("documents", mode="full")
    first = engine.state.rows(
        "SELECT id, sha256 FROM artifacts WHERE relative_path = 'handbook.pdf'"
    )
    chunks_before = engine.state.rows(
        "SELECT c.text AS text FROM chunks c JOIN artifacts a ON c.artifact_id = a.id "
        "WHERE a.relative_path = 'handbook.pdf' ORDER BY c.id"
    )
    engine.sync_source("documents", mode="full")
    second = engine.state.rows(
        "SELECT id, sha256 FROM artifacts WHERE relative_path = 'handbook.pdf'"
    )
    chunks_after = engine.state.rows(
        "SELECT c.text AS text FROM chunks c JOIN artifacts a ON c.artifact_id = a.id "
        "WHERE a.relative_path = 'handbook.pdf' ORDER BY c.id"
    )
    assert [dict(r) for r in first] == [dict(r) for r in second]
    assert [r["text"] for r in chunks_before] == [r["text"] for r in chunks_after]


# ---------------------------------------------------------------------------
# WASM-sandboxed PDF tokenizer
# ---------------------------------------------------------------------------

wasmtime = pytest.importorskip("wasmtime")


def test_wasm_guest_matches_python_byte_for_byte_on_the_fixture() -> None:
    """A sandbox cannot catch a wrong answer, so parity is asserted directly."""
    from pheasant.ingestion.extractor_sandbox import (
        extract_pdf_text_sandboxed,
        scan_pdf_content_stream_wasm,
    )

    content = HANDBOOK.read_bytes()
    assert extract_pdf_text_sandboxed(content) == extract_pdf_text_builtin(content)
    streams = iter_pdf_content_streams(content)
    assert streams  # the fixture really does have streams to compare
    for stream in streams:
        assert scan_pdf_content_stream_wasm(stream) == scan_pdf_content_stream(stream)


def test_wasm_guest_matches_python_on_adversarial_operator_streams() -> None:
    """Randomized parity over the shapes a hostile PDF would actually use.

    Covers what the fixture cannot: unterminated strings, undefined cp1252
    bytes, UTF-16 literals, deep paren nesting, odd-length hex, raw binary.
    Python's ``chr(b).isalpha()`` is true for 65 bytes above 0x7F, so an
    ASCII-only port would diverge here rather than on clean input.
    """
    from pheasant.ingestion.extractor_sandbox import scan_pdf_content_stream_wasm

    fragments = [
        b"BT ",
        b"ET ",
        b"(hello) Tj ",
        b"[(a) -20 (b)] TJ ",
        b"<48656C6C6F> Tj ",
        b"1 0 0 1 72 720 Tm ",
        b"Td ",
        b"TD ",
        b"T* ",
        b"(esc \\( \\) \\\\ \\n \\101) Tj ",
        b"% comment\n",
        b"(unterminated ",
        b"<ABC> Tj ",
        b"(\xfe\xff\x00H\x00i) Tj ",
        b"(\x80\x81\x9f\xa0\xff) Tj ",
        b"' ",
        b'" ',
        b"/F1 12 Tf ",
        b"(deep ((nested)) ok) Tj ",
        b"<> Tj ",
        b"(",
        b")",
    ]
    rng = random.Random(1337)
    for trial in range(120):
        data = b"".join(rng.choice(fragments) for _ in range(rng.randint(1, 30)))
        if trial % 5 == 0:
            data += bytes(rng.randrange(256) for _ in range(rng.randint(1, 48)))
        assert scan_pdf_content_stream_wasm(data) == scan_pdf_content_stream(data), data


def test_sandboxed_extractor_is_searchable_end_to_end(tmp_path: Path) -> None:
    engine = _make_document_engine(tmp_path, provider="sandboxed")
    from pheasant.ingestion.extractor_sandbox import SandboxedExtractor

    assert isinstance(engine.extractor, SandboxedExtractor)
    engine.sync_source("documents", mode="full")
    assert any("handbook.pdf" in p for p in _search(engine, "ZEBRAFISH protocol resync"))


def test_sandbox_fuel_cap_fails_closed(monkeypatch: Any) -> None:
    """The cap is load-bearing, not decorative: starve it and it must trap."""
    import pheasant.ingestion.extractor_sandbox as sandbox
    from pheasant.sandbox.wasm_runtime import SandboxError

    monkeypatch.setattr(sandbox, "SANDBOX_FUEL", 20_000)
    with pytest.raises(SandboxError):
        sandbox.scan_pdf_content_stream_wasm(b"BT (some text here) Tj ET " * 4000)


def test_hostile_stream_costs_only_its_own_text(monkeypatch: Any) -> None:
    """A trapping stream is skipped; the rest of the document still indexes."""
    import pheasant.ingestion.extractor_sandbox as sandbox
    from pheasant.sandbox.wasm_runtime import SandboxFuelExhausted

    calls = {"n": 0}
    real = sandbox.scan_pdf_content_stream_wasm

    def flaky(stream: bytes) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SandboxFuelExhausted("simulated hostile stream")
        return real(stream)

    monkeypatch.setattr(sandbox, "scan_pdf_content_stream_wasm", flaky)
    text = sandbox.extract_pdf_text_sandboxed(HANDBOOK.read_bytes())
    assert "MARMOSET" in text  # page 2 survived page 1 trapping
    assert "ZEBRAFISH" not in text


def test_sandboxed_provider_refuses_to_run_unsandboxed(monkeypatch: Any) -> None:
    """Failing loudly beats silently providing no isolation.

    Unlike the 34.5 accelerators — where a WASM failure correctly falls back to
    Python because acceleration is only a performance path — an operator who
    wrote ``provider: sandboxed`` asked for a security property.
    """
    import pheasant.ingestion.extractor_sandbox as sandbox
    from pheasant.sandbox.wasm_runtime import WasmRuntimeUnavailable

    sandbox.reset_for_tests()
    monkeypatch.setattr(sandbox, "wasmtime", None)
    try:
        with pytest.raises(WasmRuntimeUnavailable, match="wasm"):
            build_extractor(ExtractorSettings(provider="sandboxed"))
    finally:
        sandbox.reset_for_tests()


def test_guest_declares_no_imports_so_it_has_no_ambient_capabilities() -> None:
    """34.3's guarantee, restated for this guest: zero host surface.

    The extraction path instantiates with an empty ``Linker``. If the vendored
    module ever grew an import (a WASI syscall, a host callback), that
    instantiation would fail — so this asserts the module needs nothing.
    """
    import pheasant.ingestion.extractor_sandbox as sandbox

    engine, module = sandbox._engine_and_module()
    assert list(module.imports) == []
    linker = wasmtime.Linker(engine)
    store = wasmtime.Store(engine)
    store.set_fuel(sandbox.SANDBOX_FUEL)
    linker.instantiate(store, module)  # must not raise


# ---------------------------------------------------------------------------
# The runtime-registered source. Found by validating against a real
# producer-generated PDF through the HTTP API rather than through SyncEngine
# directly — the two disagreed, and the API path was the broken one.


def _api_client(tmp_path: Path, fixture: Path, pattern: str):
    """An app whose config has **no sources**, mirroring a real deployment
    before anyone adds one."""
    import shutil

    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    workspace = tmp_path / "workspace" / "docs"
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copy(fixture, workspace / fixture.name)
    # Deliberately NOT copying the authored `.caption.txt`/`.transcript.txt`
    # sidecar: a sidecar supplies text whether or not a handler was built, so
    # a test that copied it would pass with the fix reverted. The stub
    # captioner/transcriber is deterministic and offline, so the handler itself
    # is what has to run here.
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "runtime-source",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [],
        }
    )
    client = TestClient(create_app(config))
    response = client.post(
        "/sources",
        json={
            "name": "docs",
            "type": "document_folder",
            "path": str(workspace),
            "include": [pattern],
            "sync_now": True,
            "wait": True,
        },
    )
    assert response.status_code == 200, response.text
    return client


def test_a_pdf_source_registered_at_runtime_still_extracts_text(tmp_path: Path) -> None:
    """The regression this exists for.

    ``extractor_from_config`` runs once in ``SyncEngine.__init__`` and asks
    whether *any configured* source admits a document extension. A source added
    through ``POST /sources`` — the UI's add-source flow, and how most sources
    actually arrive — did not exist when the engine was built, so no extractor
    was constructed and its PDFs were indexed with **no text at all**: the exact
    silent-empty-content failure this module exists to fix, on the path most
    people use. Indexing the same file via ``SyncEngine`` directly worked, which
    is why the unit tests never saw it.
    """
    client = _api_client(tmp_path, HANDBOOK, "**/*.pdf")
    assert client.get("/overview").json()["chunk_count"] > 0

    hits = client.post("/search", json={"query": "handbook", "mode": "text"}).json()
    assert hits["results"], "the document's content must be searchable, not just its path"


def test_an_image_source_registered_at_runtime_is_still_captioned(tmp_path: Path) -> None:
    """Same constructor-time check, same gap — but a milder symptom, and the
    assertion has to be chosen to see it.

    Unlike a document, an image never went *empty*: `caption_to_text` falls back
    to a filename-derived caption ("Image diagram.") when no captioner was
    built, so chunk count alone stays positive either way and would pass with
    the fix reverted. What is actually lost is the captioner's own output, so
    that is what this asserts — the stub's content fingerprint, which the
    fallback has no way to produce.
    """
    client = _api_client(
        tmp_path, Path("tests/fixtures/sample_workspace/images/diagram.png"), "**/*.png"
    )
    hits = client.post("/search", json={"query": "diagram", "mode": "text"}).json()["results"]
    text = " ".join(
        chunk.get("text_preview", "") for hit in hits for chunk in hit.get("chunks", [])
    )
    assert "fingerprint" in text, f"captioner did not run; got the filename fallback: {text!r}"


def test_an_audio_source_registered_at_runtime_is_still_transcribed(tmp_path: Path) -> None:
    """And the transcriber, with the same filename-fallback caveat as above."""
    client = _api_client(
        tmp_path, Path("tests/fixtures/sample_workspace/audio/briefing.wav"), "**/*.wav"
    )
    hits = client.post("/search", json={"query": "briefing", "mode": "text"}).json()["results"]
    text = " ".join(
        chunk.get("text_preview", "") for hit in hits for chunk in hit.get("chunks", [])
    )
    assert "fingerprint" in text, f"transcriber did not run; got the fallback: {text!r}"
