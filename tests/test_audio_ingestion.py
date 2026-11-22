"""Acceptance tests for multi-modal audio ingestion (Synapse 25.4 session B).

Fully offline: the deterministic ``StubTranscriber`` turns audio bytes into
indexable text (an authored ``<audio>.transcript.txt`` sidecar wins), which
then flows through the normal chunk -> embed -> graph path. No network, no
audio decoder, no ASR model.

Session B is **audio only** — image is session A (``test_image_ingestion.py``)
and is intentionally untouched here. This mirrors the image acceptance shape
with a transcriber instead of a captioner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.ingestion.transcriber import StubTranscriber, build_transcriber
from pheasant.search.hybrid import HybridSearch
from pheasant.search.text_store import SearchStore
from pheasant.sync.engine import SyncEngine

FIXTURE_AUDIO = Path(__file__).parent / "fixtures" / "sample_workspace" / "audio" / "briefing.wav"
FIXTURE_SIDECAR = FIXTURE_AUDIO.with_name(FIXTURE_AUDIO.name + ".transcript.txt")


def _make_audio_engine(tmp_path: Path, *, with_embeddings: bool = False) -> SyncEngine:
    audio = tmp_path / "workspace" / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    (audio / "briefing.wav").write_bytes(FIXTURE_AUDIO.read_bytes())
    (audio / "briefing.wav.transcript.txt").write_text(
        FIXTURE_SIDECAR.read_text(encoding="utf-8"), encoding="utf-8"
    )
    # A second clip with no sidecar exercises the deterministic template path.
    (audio / "untitled_clip.wav").write_bytes(FIXTURE_AUDIO.read_bytes() + b"\x01")

    search: dict[str, Any] = {}
    if with_embeddings:
        search = {
            "embeddings": {"enabled": True, "provider": "stub", "dimensions": 64},
            "vector_store": {"provider": "numpy"},
        }
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "audio-acceptance",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "search": search,
            "sources": [
                {
                    "name": "audio",
                    "type": "document_folder",
                    "path": str(audio),
                    "include": ["**/*.wav", "**/*.md"],
                }
            ],
        }
    )
    return SyncEngine(config)


def _search(engine: SyncEngine, query: str, mode: str = "text") -> list[str]:
    searcher = HybridSearch(SearchStore(engine.state), vector=None)
    payload = searcher.search_context(
        engine.config.knowledge_base_id, query, mode=mode, max_results=5
    )
    return [str(item.get("relative_path") or "") for item in payload["results"]]


# ---------------------------------------------------------------------------
# Transcriber unit behavior
# ---------------------------------------------------------------------------


def test_stub_transcriber_is_deterministic_and_sidecar_wins() -> None:
    tx = StubTranscriber()
    a = tx.transcribe(b"audio-bytes", "briefing.wav")
    b = tx.transcribe(b"audio-bytes", "briefing.wav")
    assert a == b  # deterministic per bytes
    assert tx.transcribe(b"other-bytes", "briefing.wav") != a  # bytes discriminate

    authored = tx.transcribe(b"audio-bytes", "briefing.wav", sidecar=b"Hello world.")
    assert authored == "Hello world."


def test_build_transcriber_provider_selection() -> None:
    from pheasant.config.schema import TranscriberSettings

    assert isinstance(build_transcriber(TranscriberSettings(provider="stub")), StubTranscriber)
    with pytest.raises(ValueError, match="Unsupported"):
        build_transcriber(TranscriberSettings(provider="bogus"))


# ---------------------------------------------------------------------------
# Region ingest + self-search (acceptance)
# ---------------------------------------------------------------------------


def test_audio_transcript_is_searchable(tmp_path: Path) -> None:
    engine = _make_audio_engine(tmp_path)
    engine.sync_source("audio", mode="full")

    # The artifact is typed "audio".
    rows = engine.state.rows("SELECT relative_path, type FROM artifacts ORDER BY relative_path")
    by_path = {row["relative_path"]: row["type"] for row in rows}
    assert by_path.get("briefing.wav") == "audio"

    # The authored transcript is searchable via the region's own hybrid search.
    hits = _search(engine, "quarterly product briefing knowledge base freshness")
    assert any("briefing.wav" in path for path in hits), hits


def test_resync_unchanged_audio_performs_zero_retranscribe(tmp_path: Path) -> None:
    engine = _make_audio_engine(tmp_path)
    engine.sync_source("audio", mode="full")
    assert isinstance(engine.transcriber, StubTranscriber)
    baseline = engine.transcriber.calls
    assert baseline >= 2  # two clips transcribed on the full sync

    # An incremental re-sync of unchanged audio transcribes nothing: the
    # engine's pre-read sha256 skip never reaches the transcriber (zero-work,
    # like 21.4's zero re-embed and 25.4A's zero re-caption).
    engine.sync_source("audio", mode="incremental")
    assert engine.transcriber.calls == baseline


def test_text_only_region_builds_no_transcriber(tmp_path: Path) -> None:
    notes = tmp_path / "workspace" / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "a.md").write_text("# Notes\n\nplain text only.\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "text-only-audio",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [{"name": "notes", "type": "markdown_folder", "path": str(notes)}],
        }
    )
    engine = SyncEngine(config)
    assert engine.transcriber is None  # no audio globs -> no transcriber, no network risk


# ---------------------------------------------------------------------------
# Contract modalities (router will filter on this)
# ---------------------------------------------------------------------------
