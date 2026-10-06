"""What this knowledge base holds: its sources, its documents, their counts.

Every other read operation answers a question *about the content*. These
answer a question about the knowledge base itself, such as "which sources
are there", "list the PDFs" or "how many documents are indexed". Search
cannot answer those. A listing is not a ranking, and asking a retriever
"list all documents" returns the eight passages that best match the words
"list", "all" and "documents".

Two operations, both read-only and both cheap enough for a request path:

* :func:`overview` — identity, per-source counts and the extensions in play;
* :func:`documents` — the indexed documents, filtered and paged.

Both surfaces expose them (MCP ``describe_knowledge_base`` / ``list_documents``,
HTTP ``GET /knowledge-base/overview`` / ``GET /documents``), and so does the
assistant: a question it reads as being about the knowledge base itself is
answered from these functions rather than from a search
(``assistant.inventory``). One implementation, three callers.

What a listing leaves out, and why:

* **Memory records.** They are source content, but a record is an assertion
  rather than a document, and a user- or session-scoped record is readable only
  by its writer. ``memory_list`` is where records are listed, under the memory
  policy.
* **Internal sources** (a ``__`` prefix, such as the readiness probe's scratch
  source) and **removed sources**. The first is plumbing and the second is
  gone.
* **What the principal may not read**, when ``security.acl_enforced`` is on. A
  count includes only what the caller could open. Otherwise the totals would
  leak what the listing withholds.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from pheasant.ingestion.content_types import MEMORY_ARTIFACT_TYPE
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, SourceNotFound

#: Documents one call may return. A listing past this is a scroll nobody reads
#: and a payload an agent pays for. Page with ``offset`` instead.
MAX_DOCUMENTS = 500
#: Extensions an overview names before it folds the rest into "other".
MAX_EXTENSIONS = 12
ORDERS = ("path", "recent")

ARTIFACT_SCOPE = (
    "a.type <> ? AND substr(a.source_id, 1, 2) <> '__' "
    "AND NOT EXISTS (SELECT 1 FROM removed_sources r WHERE r.source_id = a.source_id)"
)


@dataclass(frozen=True)
class DocumentsRequest:
    """Which documents to list, in the vocabulary both surfaces speak."""

    knowledge_base: str | None = None
    source_name: str | None = None
    #: File extensions to keep, with or without the dot (``pdf`` or ``.pdf``).
    extensions: list[str] = field(default_factory=list)
    #: A case-insensitive substring of the relative path.
    path_contains: str | None = None
    #: ``path`` (by source, then path) or ``recent`` (last indexed first).
    order: str = "path"
    limit: int = 50
    offset: int = 0
    principal: str | None = None
    principal_groups: list[str] | None = None


def documents(context: ServiceContext, request: DocumentsRequest) -> dict[str, Any]:
    """The indexed documents matching ``request``, with the total they page over."""

    kb_id = context.knowledge_base(request.knowledge_base)
    if request.order not in ORDERS:
        raise InvalidRequest(f"order must be one of {', '.join(ORDERS)}; got {request.order!r}")
    limit = max(1, min(int(request.limit), MAX_DOCUMENTS))
    offset = max(0, int(request.offset))
    if request.source_name and not context.state.get_source(request.source_name):
        raise SourceNotFound(request.source_name)
    extensions = normalize_extensions(request.extensions)

    where = [ARTIFACT_SCOPE]
    params: list[Any] = [MEMORY_ARTIFACT_TYPE]
    if request.source_name:
        where.append("a.source_id = ?")
        params.append(request.source_name)
    if extensions:
        # The patterns go in as parameters. A literal `%` in the statement text
        # is a placeholder to psycopg.
        where.append("(" + " OR ".join("LOWER(a.relative_path) LIKE ?" for _ in extensions) + ")")
        params.extend(f"%{extension}" for extension in extensions)
    if request.path_contains and request.path_contains.strip():
        where.append("LOWER(a.relative_path) LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(request.path_contains.strip().lower())}%")
    order = (
        "a.source_id, a.relative_path"
        if request.order == "path"
        else "(a.last_indexed_at IS NULL), a.last_indexed_at DESC, a.relative_path"
    )
    clause = " AND ".join(where)
    columns = "a.id, a.source_id, a.relative_path, a.type, a.size_bytes, a.last_indexed_at"

    admit = reader(context, request.principal, request.principal_groups)
    if admit is None:
        total = int(
            context.state.rows(
                f"SELECT COUNT(*) AS n FROM artifacts a WHERE {clause}", tuple(params)
            )[0]["n"]
        )
        rows = context.state.rows(
            f"SELECT {columns} FROM artifacts a WHERE {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
    else:
        # Enforcement is per artifact and is decided in Python, so the page is
        # cut after the filter. Cutting it before would hand back short pages
        # and a total counting documents the caller cannot open.
        readable = admit(
            context.state.rows(
                f"SELECT {columns}, a.acl FROM artifacts a WHERE {clause} ORDER BY {order}",
                tuple(params),
            )
        )
        total = len(readable)
        rows = readable[offset : offset + limit]

    listed = [document_row(row) for row in rows]
    return {
        "knowledge_base": kb_id,
        "documents": listed,
        "total": total,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "returned": len(listed),
            "has_more": offset + len(listed) < total,
        },
        "filters": {
            "source_name": request.source_name,
            "extensions": extensions,
            "path_contains": (request.path_contains or "").strip() or None,
            "order": request.order,
        },
    }


def overview(
    context: ServiceContext,
    knowledge_base: str | None = None,
    principal: str | None = None,
    principal_groups: list[str] | None = None,
) -> dict[str, Any]:
    """Identity, sources with their document counts, and the extensions in play.

    One narrow scan of ``artifacts`` (four short columns) rather than a set of
    ``GROUP BY`` statements. Extensions are not a column, and grouping in SQL
    cannot apply a per-artifact ACL. A listing question is rare enough that
    O(documents) on the request is the right cost, and it never runs on a
    commit path.
    """

    kb_id = context.knowledge_base(knowledge_base)
    from pheasant.registry.source_registry import SourceRegistry

    registered = [
        row
        for row in SourceRegistry(context.config, context.state).list_sources(limit=10_000)
        if not str(row.get("name") or "").startswith("__")
    ]
    rows = context.state.rows(
        "SELECT a.id, a.source_id, a.relative_path, a.size_bytes, a.last_indexed_at"
        + (", a.acl" if context.config.security.acl_enforced else "")
        + f" FROM artifacts a WHERE {ARTIFACT_SCOPE}",
        (MEMORY_ARTIFACT_TYPE,),
    )
    admit = reader(context, principal, principal_groups)
    if admit is not None:
        rows = admit(rows)

    per_source: Counter[str] = Counter()
    bytes_per_source: Counter[str] = Counter()
    latest: dict[str, str] = {}
    extensions: Counter[str] = Counter()
    for row in rows:
        source = str(row["source_id"])
        per_source[source] += 1
        bytes_per_source[source] += int(row["size_bytes"] or 0)
        indexed = row["last_indexed_at"]
        if indexed and str(indexed) > latest.get(source, ""):
            latest[source] = str(indexed)
        extensions[extension_of(row["relative_path"])] += 1

    sources = []
    for row in registered:
        name = str(row["name"])
        sources.append(
            {
                "name": name,
                "type": row.get("type"),
                "enabled": bool(row.get("enabled")),
                "status": row.get("last_status"),
                "last_indexed_at": row.get("last_indexed_at") or latest.get(name),
                "documents": per_source.get(name, 0),
                "size_bytes": bytes_per_source.get(name, 0),
            }
        )
    # Ties broken by name, not by `most_common`'s insertion order: that is the
    # scan order, which differs between backends and between two replicas.
    common = sorted(extensions.items(), key=lambda item: (-item[1], item[0]))[:MAX_EXTENSIONS]
    folded = sum(extensions.values()) - sum(count for _, count in common)
    by_extension = [{"extension": ext, "documents": count} for ext, count in common]
    if folded:
        by_extension.append({"extension": "other", "documents": folded})
    return {
        "knowledge_base": kb_id,
        "name": context.config.pheasant.name,
        "description": context.config.pheasant.description,
        "sources": sources,
        "totals": {
            "sources": len(sources),
            "documents": len(rows),
            "size_bytes": sum(bytes_per_source.values()),
        },
        "by_extension": by_extension,
        "last_indexed_at": max(latest.values(), default=None),
    }


def normalize_extensions(raw: Any) -> list[str]:
    """``["PDF", ".md"]`` → ``[".pdf", ".md"]``, deduplicated and in order."""

    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [part for part in raw.replace(",", " ").split() if part]
    out: list[str] = []
    for value in raw:
        text = str(value or "").strip().lower()
        if not text:
            continue
        text = text if text.startswith(".") else f".{text}"
        if not text[1:].replace("_", "").replace("-", "").isalnum():
            raise InvalidRequest(f"not a file extension: {value!r}")
        if text not in out:
            out.append(text)
    return out


def extension_of(path: Any) -> str:
    suffix = PurePosixPath(str(path or "")).suffix.lower()
    return suffix or "(none)"


def document_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source": row["source_id"],
        "path": row["relative_path"],
        "type": row["type"],
        "extension": extension_of(row["relative_path"]),
        "size_bytes": row["size_bytes"],
        "last_indexed_at": row["last_indexed_at"],
    }


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def reader(context: ServiceContext, principal: str | None, groups: list[str] | None) -> Any:
    """A filter keeping the rows ``principal`` may read, or ``None`` if ACLs are off.

    The rule ``services.graph.require_readable`` applies to one artifact,
    applied to a list. A row carries its own ``acl`` column, so this needs no
    second lookup.
    """

    security = context.config.security
    if not security.acl_enforced:
        return None
    from pheasant.security.acl import is_allowed
    from pheasant.services.graph import _identities

    identities = _identities(context, principal, groups)
    default_public = security.default_visibility != "private"

    def admit(rows: list[Any]) -> list[Any]:
        return [
            row for row in rows if is_allowed(row["acl"], identities, default_public=default_public)
        ]

    return admit
