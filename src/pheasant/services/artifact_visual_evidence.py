"""Structured diagrams share the enclosing artifact's evidence numbering."""

from __future__ import annotations

from typing import Any


def prepare(payload: dict) -> None:
    if not isinstance(payload.get("visual"), dict) or "visual_citation_map" in payload:
        return
    from pheasant.services.artifact_evidence import identity

    citations = payload.setdefault("citations", [])
    visual = list(payload.get("visual_citations") or citations)
    next_index = max([0, *(int(c["index"]) for c in citations if str(c.get("index")).isdigit())])
    mapping = {}
    for citation in visual:
        matched = next((c for c in citations if identity(c) == identity(citation)), None)
        if matched is None:
            # Captured evidence may have gained a revision. An unqualified
            # visual can use it only when its location has one unambiguous match.
            locations = [
                c
                for c in citations
                if all(
                    c.get(key) == citation.get(key)
                    for key in ("source", "path", "section", "chunk_id")
                )
            ]
            if not citation.get("revision") and len(locations) == 1:
                matched = locations[0]
        if matched is None:
            next_index += 1
            matched = {**citation, "index": next_index}
            citations.append(matched)
        mapping[str(citation.get("index"))] = str(matched["index"])
    payload["visual_citation_map"] = mapping


def renumber(payload: dict, mapping: dict[str, int]) -> None:
    visual = payload.get("visual")
    if not isinstance(visual, dict):
        return
    local = {
        key: mapping[value]
        for key, value in payload.get("visual_citation_map", {}).items()
        if value in mapping
    }
    payload["visual_citation_map"] = local
    used = set(local.values())
    payload["visual_citations"] = [
        {key: value for key, value in c.items() if key != "snapshot"}
        for c in payload["citations"]
        if c["index"] in used
    ]
    unresolved: set[str] = set()
    counts = {"cited": 0, "inferred": 0}

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, dict):
            for key, item in list(value.items()):
                if key in {"cites", "citations"} and isinstance(item, list):
                    numbers = [str(number).strip("[]") for number in item]
                    unresolved.update(n for n in numbers if n not in local)
                    value[key] = list(dict.fromkeys(local[n] for n in numbers if n in local))
                    if key == "cites":
                        counts["cited" if value[key] else "inferred"] += 1
                        if item and not value[key]:
                            value["inferred"] = True
                else:
                    walk(item)

    walk(visual)
    if unresolved:
        visual["unresolved_citations"] = sorted(unresolved)
        total = sum(counts.values())
        visual["grounding"] = {**counts, "ratio": counts["cited"] / total if total else 0}
