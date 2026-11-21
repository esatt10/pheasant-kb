"""Acceptance tests for multi-modal image ingestion (Synapse 25.4 session A).

Fully offline: the deterministic ``StubCaptioner`` turns image bytes into
indexable text (an authored ``<image>.caption.txt`` sidecar wins), which then
flows through the normal chunk -> embed -> graph path. No network, no image
decoder, no model.

Session A is **image only** — audio is session B and is intentionally absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.ingestion.captioner import StubCaptioner, build_captioner
from pheasant.search.hybrid import HybridSearch
from pheasant.search.sqlite_store import SearchStore
from pheasant.sync.engine import SyncEngine

FIXTURE_IMAGE = Path(__file__).parent / "fixtures" / "sample_workspace" / "images" / "diagram.png"
FIXTURE_SIDECAR = FIXTURE_IMAGE.with_name(FIXTURE_IMAGE.name + ".caption.txt")


def _make_image_engine(tmp_path: Path, *, with_embeddings: bool = False) -> SyncEngine:
    images = tmp_path / "workspace" / "images"
    images.mkdir(parents=True, exist_ok=True)
    (images / "diagram.png").write_bytes(FIXTURE_IMAGE.read_bytes())
    (images / "diagram.png.caption.txt").write_text(
        FIXTURE_SIDECAR.read_text(encoding="utf-8"), encoding="utf-8"
    )
    # A second image with no sidecar exercises the deterministic template path.
    (images / "untitled_photo.png").write_bytes(FIXTURE_IMAGE.read_bytes() + b"\x00")

    search: dict[str, Any] = {}
    if with_embeddings:
        search = {
            "embeddings": {"enabled": True, "provider": "stub", "dimensions": 64},
            "vector_store": {"provider": "numpy"},
        }
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "image-acceptance",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path / "workspace"),
                "exports_path": str(tmp_path / "exports"),
            },
            "search": search,
            "sources": [
                {
                    "name": "images",
                    "type": "document_folder",
                    "path": str(images),
                    "include": ["**/*.png", "**/*.md"],
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
# Captioner unit behavior
# ---------------------------------------------------------------------------


def test_stub_captioner_is_deterministic_and_sidecar_wins() -> None:
    cap = StubCaptioner()
    a = cap.caption(b"image-bytes", "diagram.png")
    b = cap.caption(b"image-bytes", "diagram.png")
    assert a == b  # deterministic per bytes
    assert cap.caption(b"other-bytes", "diagram.png") != a  # bytes discriminate

    authored = cap.caption(b"image-bytes", "diagram.png", sidecar=b"A red square.")
    assert authored == "A red square."


def test_build_captioner_provider_selection() -> None:
    from pheasant.config.schema import CaptionerSettings

    assert isinstance(build_captioner(CaptionerSettings(provider="stub")), StubCaptioner)
    with pytest.raises(ValueError, match="Unsupported"):
        build_captioner(CaptionerSettings(provider="bogus"))


# ---------------------------------------------------------------------------
# Region ingest + self-search (acceptance)
# ---------------------------------------------------------------------------


def test_image_caption_is_searchable(tmp_path: Path) -> None:
    engine = _make_image_engine(tmp_path)
    engine.sync_source("images", mode="full")

    # The artifact is typed "image".
    rows = engine.state.rows("SELECT relative_path, type FROM artifacts ORDER BY relative_path")
    by_path = {row["relative_path"]: row["type"] for row in rows}
    assert by_path.get("diagram.png") == "image"

    # The authored caption is searchable via the region's own hybrid search.
    hits = _search(engine, "architecture diagram router regions")
    assert any("diagram.png" in path for path in hits), hits


def test_resync_unchanged_image_performs_zero_recaption(tmp_path: Path) -> None:
    engine = _make_image_engine(tmp_path)
    engine.sync_source("images", mode="full")
    assert isinstance(engine.captioner, StubCaptioner)
    baseline = engine.captioner.calls
    assert baseline >= 2  # two images captioned on the full sync

    # An incremental re-sync of unchanged images captions nothing: the engine's
    # pre-read sha256 skip never reaches the captioner (zero-work, like 21.4's
    # zero re-embed).
    engine.sync_source("images", mode="incremental")
    assert engine.captioner.calls == baseline


def test_text_only_region_builds_no_captioner(tmp_path: Path) -> None:
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
            "sources": [{"name": "notes", "type": "markdown_folder", "path": str(notes)}],
        }
    )
    engine = SyncEngine(config)
    assert engine.captioner is None  # no image globs -> no captioner, no network risk


# ---------------------------------------------------------------------------
# Contract modalities (router will filter on this)
# ---------------------------------------------------------------------------
