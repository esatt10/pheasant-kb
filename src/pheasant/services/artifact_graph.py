"""Describe typed artifact links with work proportional to their own edges."""

from __future__ import annotations

from typing import Any


def coverage(graph: Any, artifact_ids: list[str]) -> dict:
    from pheasant.artifacts.bridge import ABOUT_EDGE

    artifacts = sorted(set(artifact_ids))
    remote = getattr(graph, "remote_memory_coverage", None)
    if callable(remote):
        return remote(artifact_ids=artifacts)
    bridged: set[str] = set()
    by_signal: dict[str, int] = {}
    with graph.reading():
        for edges in graph.out_edges_batch(artifacts).values():
            for source, _target, edge_map in edges:
                for data in edge_map.values():
                    if data.get("type") == ABOUT_EDGE:
                        bridged.add(source)
                        signal = str(data.get("match_signal") or "unknown")
                        by_signal[signal] = by_signal.get(signal, 0) + 1
    return {
        "records": len(artifacts),
        "bridged": len(bridged),
        "unbridged": len(artifacts) - len(bridged),
        "by_signal": by_signal,
    }
