"""Retire memories grounded in a corpus source when that source is removed."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from pheasant.memory.store import MemoryStore, memory_source

SOURCE_REMOVAL_MARKER = ".source-removal-pending"


def retire_source_memories(engine: Any, source_name: str) -> dict[str, int]:
    """Archive sole-source memories and correct composites before graph removal.

    Only existing memory-to-corpus links count as support. Unlinked memories
    (including personal notes) are left alone; sharing a word with a source
    name is not evidence that the memory depends on that source.
    """
    source = memory_source(engine.config, engine.state)
    if source is None or source.name == source_name:
        return {"archived": 0, "corrected": 0}

    records = MemoryStore(source.path)
    all_records = records.list_records()
    prior = {record.record_id: record for record in all_records}
    recovery_tag = f"deprecated-source:{quote(source_name, safe='')}"
    interrupted = {
        record.supersedes
        for record in all_records
        if recovery_tag in record.tags
        and record.supersedes
        and record.supersedes in prior
    }
    recovered = 0
    if interrupted:
        (Path(source.path) / SOURCE_REMOVAL_MARKER).touch(exist_ok=True)
        for record_id in sorted(interrupted):
            MemoryStore.archive(prior[record_id])
            recovered += 1
    live_records = records.list_records()
    by_id = {record.record_id: record for record in live_records}
    superseded = records.superseded_ids(live_records)
    if not by_id:
        return {"archived": recovered, "corrected": 0}

    artifact_to_record = {
        str(row["artifact_id"]): str(row["record_id"])
        for row in engine.state.rows(
            "SELECT artifact_id, record_id FROM memory_records WHERE source_id=?",
            (source.name,),
        )
    }
    support: dict[str, set[str]] = {}
    members: dict[str, set[str]] = {}
    graph = engine.graph_builder.graph
    with graph.reading():
        nodes = graph.node_map()
        for (origin, target), edges in graph.iter_edges():
            record_id = artifact_to_record.get(origin)
            if record_id is None:
                continue
            if any(edge.get("type") == "subsumes" for edge in edges.values()):
                member_id = artifact_to_record.get(target)
                if member_id is not None:
                    members.setdefault(record_id, set()).add(member_id)
                continue
            if not any(
                edge.get("type") in {"about", "references", "imports"}
                for edge in edges.values()
            ):
                continue
            attrs = nodes.get(target) or {}
            if attrs.get("type") == "external_reference":
                continue
            target_source = str(attrs.get("source_id") or "")
            if target_source and target_source != source.name:
                support.setdefault(record_id, set()).add(target_source)

    for record_id, record in by_id.items():
        for tag in record.tags:
            if tag.startswith("source-support:"):
                support.setdefault(record_id, set()).add(unquote(tag.split(":", 1)[1]))

    live_sources = {
        str(row["id"])
        for row in engine.state.rows(
            "SELECT id FROM sources WHERE id NOT IN "
            "(SELECT source_id FROM removed_sources)"
        )
    }

    # A synthesized or compacted memory also inherits the support of the
    # records it subsumed. Otherwise a composite can lose its only explicit
    # links while its member records still identify both corpus sources.
    def sources_for(record_id: str, seen: set[str]) -> set[str]:
        if record_id in seen:
            return set()
        result = set(support.get(record_id, ()))
        for member_id in members.get(record_id, ()):
            result.update(sources_for(member_id, seen | {record_id}))
        return result

    affected = []
    for record_id, record in sorted(by_id.items()):
        sources = sources_for(record_id, set())
        if source_name in sources:
            affected.append(
                (record, sorted((sources & live_sources) - {source_name, source.name}))
            )
    if not affected:
        return {"archived": recovered, "corrected": 0}

    # Keep the intent on disk before changing any record. If the process dies
    # after archiving but before reindexing, redelivery still knows to reconcile
    # the memory source even though the removed corpus nodes have disappeared.
    (Path(source.path) / SOURCE_REMOVAL_MARKER).touch(exist_ok=True)
    archived, corrected = recovered, 0
    for record, survivors in affected:
        if survivors and record.record_id not in superseded:
            note = (
                f"Source {source_name} was deprecated and removed. "
                f"Remaining source support: {', '.join(survivors)}."
            )
            provenance_tags = tuple(
                tag
                for tag in record.tags
                if not tag.startswith("source-support:") and tag != "source-deprecated"
            )
            _replacement, created = records.append(
                f"{record.text}\n\n{note}",
                scope=record.scope,
                subject=record.subject,
                supersedes=record.record_id,
                tags=(
                    *provenance_tags,
                    "source-deprecated",
                    f"deprecated-source:{quote(source_name, safe='')}",
                    *(f"source-support:{quote(name, safe='')}" for name in survivors),
                ),
                kind=record.kind,
                written_by=record.written_by,
                valid_until=record.valid_until,
            )
            if created:
                corrected += 1
        MemoryStore.archive(record)
        archived += 1
    return {"archived": archived, "corrected": corrected}
