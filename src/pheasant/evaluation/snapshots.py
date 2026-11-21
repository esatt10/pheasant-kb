"""Immutable manifests: what state was evaluated, exactly.

A snapshot is not a copy of the knowledge base. Copying a multi-million-chunk
corpus per evaluation run is not affordable and would not help: what a
comparison needs is the ability to say *whether two runs saw the same thing*,
and for that a digest is as good as the bytes and costs a scan instead of a
duplication.

So a manifest is a set of content digests over every input capable of changing
what retrieval returns. The completeness matters more than any single field.
Two runs are directly comparable only when the fields that differ between them
can be enumerated -- and a field nobody recorded cannot be enumerated, so an
uncaptured input becomes an unexplained metric change months later, which is
the failure this whole plane is built to avoid.

**Digests are computed the same way on every replica.** Every one of them is
an ordered, deterministic reduction over rows read in a fixed sort order, so
two API pods building a manifest for one state agree on its id without
coordinating. That is what makes ``save_snapshot``'s ``ON CONFLICT DO NOTHING``
the right idempotency, and it is why nothing here reads a clock, a hostname,
or a process id.

**Aggregation happens in SQL, not in Python.** ``digest_of_query`` reduces with
a running hash over a streamed cursor rather than materializing ids into a
list. A corpus manifest over 400k artifacts is a real shape here, and the
evaluation plane must never be the thing that makes a region run out of memory.

**A partial manifest says so.** A digest the builder could not resolve is
recorded in ``incomplete`` rather than defaulted to an empty string. A run over
an incomplete snapshot is invalid by default (``incomplete_snapshot_blocks_run``),
which is only enforceable if incompleteness is representable.
"""

from __future__ import annotations

import logging
from hashlib import blake2b
from typing import Any

from pheasant.evaluation.contracts import SnapshotManifest, digest, utc_now

logger = logging.getLogger(__name__)

#: Sentinel for a digest over nothing at all. Distinct from a digest that
#: could not be computed (which goes to ``incomplete``): an empty corpus is a
#: real, reproducible state, and conflating "no rows" with "no answer" would
#: make a fresh region look broken.
EMPTY_DIGEST = "0" * 16


def digest_of_query(state: Any, sql: str, params: tuple[Any, ...] = ()) -> str:
    """A stable digest over a query's rows, in the order the query returns them.

    The caller owns the ``ORDER BY``; without one the digest is a coin flip
    across backends, and a snapshot id that changes when nothing did is worse
    than no snapshot id at all.
    """

    hasher = blake2b(digest_size=8)
    seen = False
    for row in state.rows(sql, params):
        seen = True
        # The values, in column order. A row is a mapping: iterating it gives
        # the column names, which are the same for every state.
        for value in row.values():
            hasher.update(b"" if value is None else str(value).encode("utf-8"))
            hasher.update(b"\x1f")
        hasher.update(b"\x1e")
    return hasher.hexdigest() if seen else EMPTY_DIGEST


def _count(state: Any, sql: str, params: tuple[Any, ...] = ()) -> int:
    rows = state.rows(sql, params)
    return int(rows[0]["c"]) if rows else 0


def _corpus_section(state: Any) -> dict[str, Any]:
    return {
        # sha256 per artifact, in id order: the one value that changes if and
        # only if indexed *content* changed. Deliberately not `last_indexed_at`,
        # which moves on every re-sync of an unchanged corpus and would make
        # every idempotent sync look like a new state.
        "content_manifest_digest": digest_of_query(
            state, "SELECT id, sha256, status FROM artifacts ORDER BY id"
        ),
        "artifact_count": _count(state, "SELECT COUNT(*) AS c FROM artifacts"),
        "chunk_count": _count(state, "SELECT COUNT(*) AS c FROM chunks"),
        "source_manifest_digest": digest_of_query(
            state, "SELECT id, name, type, enabled, config_json FROM sources ORDER BY id"
        ),
    }


def _graph_section(state: Any, graph: Any) -> dict[str, Any]:
    """Node/edge counts and a digest of the relation vocabulary.

    The graph is a file, not a table, so this reads the live object when the
    caller has one. The digest covers the *relation schema* -- which edge types
    exist and how many of each -- rather than every edge: a per-edge digest
    over 1.5M edges on every run buys precision nobody reads, while the type
    histogram is exactly what changes when enrichment behaviour changes.
    """

    if graph is None:
        return {}
    try:
        nodes = graph.number_of_nodes()
        edges = graph.number_of_edges()
        # `type_counts` is maintained on write rather than scanned, so the node
        # half of this costs nothing on a 400k-node graph. The edge half has no
        # such counter and is walked under one `reading()` hold: re-acquiring
        # the lock per edge is measurable on a graph this size, and it is the
        # documented contract of `iter_edges`.
        node_types = dict(graph.type_counts())
        edge_types: dict[str, int] = {}
        with graph.reading():
            for _endpoints, edge_map in graph.iter_edges():
                for attrs in edge_map.values():
                    key = str((attrs or {}).get("type") or "unknown")
                    edge_types[key] = edge_types.get(key, 0) + 1
    except Exception:  # noqa: BLE001 - a manifest must not fail a run
        logger.warning("evaluation: graph section unavailable", exc_info=True)
        return {}
    return {
        "graph_digest": digest(
            sorted(node_types.items()), sorted(edge_types.items()), nodes, edges
        ),
        "node_count": int(nodes),
        "edge_count": int(edges),
        "relation_schema_digest": digest(sorted(edge_types)),
        "node_types": dict(sorted(node_types.items())),
        "edge_types": dict(sorted(edge_types.items())),
    }


def _retrieval_section(state: Any, config: Any) -> dict[str, Any]:
    search = config.search
    embeddings = getattr(search, "embeddings", None)
    vector_store = getattr(search, "vector_store", None)
    ingestion = config.ingestion
    return {
        # The lexical index is derived from chunks, so its digest is a digest
        # of what was indexed rather than of the index structure -- which is
        # dialect-specific (FTS5 virtual table vs. a tsvector column) and would
        # make a SQLite manifest and a Postgres manifest of one corpus differ.
        "lexical_index_digest": digest_of_query(
            state, "SELECT id, text_hash FROM chunks ORDER BY id"
        ),
        "vector_index_digest": (
            digest(
                getattr(embeddings, "provider", None),
                getattr(embeddings, "model", None),
                getattr(vector_store, "provider", None),
            )
            if embeddings is not None and getattr(embeddings, "enabled", False)
            else EMPTY_DIGEST
        ),
        "encoding_profile_id": (
            f"{getattr(embeddings, 'provider', 'none')}:{getattr(embeddings, 'model', 'none')}"
            if embeddings is not None
            else "none"
        ),
        "encoding_profile_digest": digest(
            getattr(embeddings, "provider", None),
            getattr(embeddings, "model", None),
            getattr(embeddings, "enabled", False),
            getattr(embeddings, "dimensions", None),
        ),
        "chunking_profile_digest": digest(
            getattr(ingestion, "chunk_size", None),
            getattr(ingestion, "chunk_overlap", None),
            getattr(ingestion, "chunk_strategy", None),
        ),
        # RRF is the fusion, and its one constant is the whole profile. Named
        # explicitly because a change to it re-orders every hybrid result in
        # the region, and a metric delta with no manifest field to blame is an
        # unexplained regression.
        "fusion_profile_digest": digest("rrf", _rrf_k()),
        "arm_limits_digest": digest(
            getattr(search, "max_results", None),
            getattr(search, "default_mode", None),
            getattr(search, "wasm_relationship_search", None),
        ),
    }


def _rrf_k() -> int:
    from pheasant.search.hybrid import RRF_K

    return int(RRF_K)


def _memory_section(state: Any, config: Any) -> dict[str, Any]:
    memory = config.memory
    try:
        records = state.rows(
            "SELECT record_id, scope, kind, valid_from, valid_until, supersedes, tier, "
            "subsumed_by FROM memory_records ORDER BY record_id"
        )
    except Exception:  # noqa: BLE001 - a region without memory has no table rows
        logger.debug("evaluation: no memory records", exc_info=True)
        records = []
    hot = sum(1 for row in records if (row["tier"] or "hot") == "hot")
    cold = sum(1 for row in records if (row["tier"] or "hot") == "cold")
    # A record another record supersedes is retained, not deleted -- that is
    # what makes `as_of` able to bring it back. Counting them separately keeps
    # "the store grew" and "the store accumulated corrections" distinguishable.
    superseded = {str(row["supersedes"]) for row in records if row["supersedes"]}
    steering = [
        row for row in records if str(row["kind"] or "") in ("alias", "preference", "exclusion")
    ]
    return {
        "record_manifest_digest": digest(
            [
                [
                    row["record_id"],
                    row["scope"],
                    row["kind"],
                    row["valid_from"],
                    row["valid_until"],
                    row["supersedes"],
                    row["tier"],
                    row["subsumed_by"],
                ]
                for row in records
            ]
        )
        if records
        else EMPTY_DIGEST,
        "current_hot_count": hot,
        "cold_count": cold,
        "retained_invalid_count": len(superseded),
        "steering_manifest_digest": digest([row["record_id"] for row in steering])
        if steering
        else EMPTY_DIGEST,
        "memory_policy_digest": digest(
            getattr(memory, "default_policy", None),
            getattr(memory, "steering_enabled", None),
            getattr(memory, "enabled", None),
            getattr(memory, "session_ttl_days", None),
        ),
    }


def _security_section(config: Any) -> dict[str, Any]:
    security = config.security
    return {
        "acl_policy_digest": digest(
            getattr(security, "acl_enforced", None),
            getattr(security, "default_visibility", None),
            sorted(str(p) for p in getattr(security, "allow_workspace_roots", []) or []),
        )
    }


def build_snapshot(
    state: Any,
    config: Any,
    *,
    graph: Any = None,
    effective_as_of: str | None = None,
    evaluation_digests: dict[str, Any] | None = None,
) -> SnapshotManifest:
    """Compute the manifest for the knowledge base as it stands right now.

    ``effective_as_of`` separates *when the manifest was written* from *what
    instant it describes*. They are the same for a current-state run and
    deliberately different for a historical reconstruction, where the manifest
    is built now but describes what the region could have known at ``t``.
    """

    created_at = utc_now()
    as_of = effective_as_of or created_at
    incomplete: list[str] = []

    def section(name: str, builder: Any) -> dict[str, Any]:
        try:
            value = builder()
        except Exception:  # noqa: BLE001 - a missing input is recorded, not raised
            logger.warning("evaluation: snapshot section %r failed", name, exc_info=True)
            incomplete.append(name)
            return {}
        if not value:
            incomplete.append(name)
        return value

    corpus = section("corpus", lambda: _corpus_section(state))
    graph_section = section("graph", lambda: _graph_section(state, graph))
    retrieval = section("retrieval", lambda: _retrieval_section(state, config))
    memory = section("memory", lambda: _memory_section(state, config))
    security = section("security", lambda: _security_section(config))
    evaluation = dict(evaluation_digests or {})
    if not evaluation:
        incomplete.append("evaluation")

    # Content-addressed over the *state*, with no clock in it. Two runs an hour
    # apart over an unchanged region produce one snapshot id, which is what
    # makes "the same snapshot and configuration produce the same result"
    # checkable rather than aspirational -- and what makes `save_snapshot`'s
    # `ON CONFLICT DO NOTHING` the right idempotency instead of an accident of
    # two runs landing in the same second.
    #
    # `effective_as_of` is deliberately **not** in the digest. A historical
    # reconstruction reads the same indexed state under an earlier proof
    # cutoff; putting its instant in the id would claim the corpus itself
    # differed, which it does not. The cutoff lives in the manifest field and
    # in the run row's `mode`, where it describes what it actually is.
    snapshot_id = "kb-" + digest(corpus, graph_section, retrieval, memory, security, evaluation)
    return SnapshotManifest(
        snapshot_id=snapshot_id,
        kb_id=config.knowledge_base_id,
        created_at=created_at,
        effective_as_of=as_of,
        corpus=corpus,
        graph=graph_section,
        retrieval=retrieval,
        memory=memory,
        security=security,
        evaluation=evaluation,
        incomplete=tuple(sorted(set(incomplete))),
    )


def material_change(previous: SnapshotManifest | None, current: SnapshotManifest) -> list[str]:
    """The manifest fields that make ``current`` worth evaluating.

    "Material" is a policy decision the specification leaves open, so this
    answers it narrowly and visibly: a change to content, graph, retrieval
    configuration, memory or ACL policy is material; counts moving without
    their digests moving is not, because a count can drift on a re-count while
    describing identical state.
    """

    if previous is None:
        return ["initial"]
    watched = {
        "corpus.content_manifest_digest",
        "corpus.source_manifest_digest",
        "graph.graph_digest",
        "retrieval.lexical_index_digest",
        "retrieval.vector_index_digest",
        "retrieval.encoding_profile_digest",
        "retrieval.chunking_profile_digest",
        "retrieval.fusion_profile_digest",
        "memory.record_manifest_digest",
        "memory.steering_manifest_digest",
        "memory.memory_policy_digest",
        "security.acl_policy_digest",
    }
    return [name for name in previous.differences(current) if name in watched]
