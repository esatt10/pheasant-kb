"""What a file-preparation worker produces, and the process worker's entry point.

The engine prepares files on a pool -- threads, processes or remote workers --
and commits them one at a time on the single coordinated writer. This module
is the half that crosses that boundary: `_PreparedItem` is the immutable
answer a worker hands back, and `_prepare_filesystem_item_process` is the
function a *process* worker runs, which is why it is a module-level function
reachable by qualified name rather than a method. Split out of `engine.py`
when it passed its size ceiling (`tests/test_module_budget.py`); nothing here
mutates authoritative state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pheasant.config.schema import SourceConfig
from pheasant.ingestion.content_types import TEXT_EXTENSIONS
from pheasant.ingestion.media import MAX_MEDIA_BYTES
from pheasant.ingestion.pipeline import ParsedArtifact, parse_connector_payload, sha256_bytes
from pheasant.sync.connectors import ConnectorItem, ConnectorPayload

if TYPE_CHECKING:
    from pheasant.sync.engine import SyncMode


@dataclass(frozen=True)
class _PreparedItem:
    """Immutable worker output consumed by the single coordinated writer."""

    position: int
    item: ConnectorItem
    previous: dict[str, Any] | None
    parsed: ParsedArtifact | None = None
    fetched: bool = False
    skipped: bool = False
    transfer_skipped: bool = False
    #: The `readiness.corpus_denylist` pattern that refused this item. Separate
    #: from `skipped` because an unchanged file and a refused one are both "not
    #: indexed" and nothing like each other — a control folded into a routine
    #: counter is one nobody can see working.
    refused_by: str | None = None
    #: An image's bytes, carried to the single writer so it can store them in
    #: the media store (`ingestion.media`). Only images, only under the store's
    #: size cap: everything else is text the chunks already hold.
    media: bytes | None = None


def _media_bytes(parsed: ParsedArtifact | None, content: bytes) -> bytes | None:
    if parsed is None or parsed.type != "image" or len(content) > MAX_MEDIA_BYTES:
        return None
    return content


_PROCESS_SAFE_TEXT_EXTENSIONS = TEXT_EXTENSIONS - {".html"}


def _process_safe_text_path(path: str) -> bool:
    candidate = Path(path)
    return (
        candidate.suffix.lower() in _PROCESS_SAFE_TEXT_EXTENSIONS
        or candidate.name.lower() == "dockerfile"
    )


def _process_cpu_capacity() -> int:
    """CPU count visible to this process (affinity/cgroup aware where available)."""

    process_cpu_count = getattr(os, "process_cpu_count", None)
    if process_cpu_count is not None:
        return max(1, int(process_cpu_count() or 1))
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except AttributeError:
        return max(1, int(os.cpu_count() or 1))


def _prepare_filesystem_item_process(
    source: SourceConfig,
    mode: SyncMode,
    git_metadata: tuple[str | None, str | None, bool] | None,
    position: int,
    item: ConnectorItem,
    previous: dict[str, Any] | None,
) -> _PreparedItem:
    """Stateless process-worker entry point for ordinary filesystem text."""

    if (
        mode == "incremental"
        and previous is not None
        and item.sha256 is not None
        and previous.get("sha256") == item.sha256
    ):
        return _PreparedItem(
            position,
            item,
            previous,
            skipped=True,
            transfer_skipped=True,
        )
    path = Path(str(item.metadata["path"]))
    content = path.read_bytes()
    content_hash = sha256_bytes(content)
    if mode == "incremental" and previous and previous.get("sha256") == content_hash:
        return _PreparedItem(
            position,
            item,
            previous,
            fetched=True,
            skipped=True,
        )
    payload = ConnectorPayload(
        item=item,
        content=content,
        mime_type=item.mime_type,
        size_bytes=item.size_bytes,
        sha256=content_hash,
        mtime=item.mtime,
        metadata={"path": str(path)},
    )
    parsed = parse_connector_payload(source, item, payload, git_metadata)
    if parsed is None:
        return _PreparedItem(position, item, previous, fetched=True)
    if mode == "incremental" and previous and previous.get("sha256") == parsed.sha256:
        return _PreparedItem(
            position,
            item,
            previous,
            parsed=parsed,
            fetched=True,
            skipped=True,
        )
    return _PreparedItem(
        position, item, previous, parsed=parsed, fetched=True, media=_media_bytes(parsed, content)
    )


def _remote_inflight_batches(workers: int, batches: int, urls: int, configured: int) -> int:
    """How many preparation batches one source keeps in flight.

    Two per URL unless ``remote_worker_max_inflight_batches`` says otherwise.
    That rule is right for a list of individual workers and blind to a URL
    that is a load-balanced Service, which is why the setting exists; either
    way it never exceeds ``max_parallel_files`` or the number of batches.
    """

    per_fleet = configured if configured > 0 else urls * 2
    return max(1, min(workers, batches, per_fleet))
