"""Projection for clients that can take actions.

Build this surface by allowlisting fields. Never copy publisher strings,
links, quotes, model context, or exception messages into an agent response.
"""

from __future__ import annotations

from collections import Counter
from hashlib import sha256
from typing import Any


def _source_id(url: str) -> str:
    return sha256(url.encode("utf-8")).hexdigest()[:24]


def summary(profile: dict[str, Any]) -> dict[str, Any]:
    fields = {}
    for key, result in profile["fields"].items():
        raw = result["value"]
        corroborated = None
        if isinstance(raw, list):
            corroborated = [
                item["value"]
                for item in result.get("items", [])
                if type(item["value"]) in {bool, int, float} and item["independent_origins"] >= 2
            ] or None
        elif type(raw) in {bool, int, float} and result.get("independent_origins", 0) >= 2:
            corroborated = raw
        has_text = isinstance(raw, str) or (
            isinstance(raw, list) and any(isinstance(entry, str) for entry in raw)
        )
        fields[key] = {
            "status": result["status"],
            "value": corroborated,
            "has_text_value": has_text,
            "proposal_count": len(result.get("proposals", [])),
            "source_ids": [_source_id(evidence["url"]) for evidence in result["evidence"]],
        }
    return {
        "as_of": profile["as_of"],
        "identity": {
            "status": profile["identity"]["status"],
            "candidate_count": len(profile["identity"]["candidates"]),
        },
        "fields": fields,
        "field_status_counts": profile["field_status_counts"],
        "research": profile.get("research"),
        "signal_counts": {kind: len(entries) for kind, entries in profile["signals"].items()},
        "quarantined": sum(source["quarantined"] for source in profile["source_outcomes"]),
        "hubspot": {kind: len(mapping) for kind, mapping in profile["hubspot"].items()},
    }


def passage_index(selected: dict[str, list[dict]]) -> dict:
    return {
        field: [
            {"id": row["id"], "source": row["source"], "source_id": _source_id(row["url"])}
            for row in rows
        ]
        for field, rows in selected.items()
    }


def token_usage(run: dict) -> dict:
    events = run.get("token_usage", [])
    tracked = not run.get("token_usage_history_incomplete") and (
        "token_usage" in run or not (run["reviewed"] or run["decision_calls"])
    )
    dimensions = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")

    def totals(rows):
        return {
            "calls": sum(row["calls"] for row in rows) if tracked else None,
            "retries": sum(row["retries"] for row in rows) if tracked else None,
            **{
                key: sum(row[key] for row in rows)
                if tracked and all(row[key] is not None for row in rows)
                else None
                for key in dimensions
            },
        }

    return {
        **totals(events),
        "stages": {
            stage: totals([row for row in events if row["stage"] == stage])
            for stage in ("review", "decisions")
        },
        "models": sorted({model for event in events for model in event["models"]}),
        "tracking_complete": tracked,
    }


def acquisition(run: dict) -> dict:
    return {
        "page_attempts": sum(task["attempts"] for task in run["tasks"]),
        "search_attempts": sum(query["attempts"] for query in run["queries"]),
        "outcomes": dict(Counter(task["outcome"] for task in run["tasks"] if "outcome" in task)),
        "document_methods": dict(
            Counter(doc.get("acquisition", "page") for doc in run["documents"].values())
        ),
    }


def storage_manifest(run: dict, checkpoint_bytes: int) -> dict:
    return {
        "checkpoint_bytes": checkpoint_bytes,
        "task_count": len(run["tasks"]),
        "documents": len(run["documents"]),
        "candidates": len(run["candidates"]),
        "review_passages": sum(len(rows) for rows in run.get("review_passages", {}).values()),
        "research": run.get("research"),
        "collection_errors": len(run["errors"]),
        "acquisition": acquisition(run),
        "token_usage": token_usage(run),
    }
