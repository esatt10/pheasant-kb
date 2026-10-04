"""Graph reads, once.

Everything here existed twice: as module-level functions in `api/app.py` and as
methods on `PheasantTools`, with the HTTP versions' own docstrings describing
them as what the MCP versions "mirror". They had drifted — see the package
docstring for the measured list — and the MCP copies papered over it by
importing the HTTP ones for the remote-graph case, which put the transport
module on the import path of the tool layer and of the assistant.

The walk itself is not here. `neighbors` and `slice_` are pure domain — a graph
in, nodes out — and live in `pheasant.graph.traversal`, where the assistant can
call them without reaching through an application layer it has no business
knowing about. They are re-exported below so a transport imports one module for
"graph reads".

What is left is what genuinely needs an application context: the ACL guard, and
the three operations that read the state store.
"""

from __future__ import annotations

from typing import Any

from pheasant.graph.traversal import neighbors, slice_
from pheasant.services import ServiceContext
from pheasant.services.errors import NotPermitted

#: Re-exported so both transports import one module for "graph reads", while
#: the walk itself stays in the domain where the assistant can reach it too.
__all__ = [
    "explain_node",
    "file_summary",
    "neighbor_filter",
    "neighbors",
    "repo_map",
    "require_readable",
    "slice_",
]


def explain_node(context: ServiceContext, node_id: str) -> dict[str, Any]:
    """What a node is, in one sentence, plus its attributes.

    The MCP copy omitted ``node`` — so an agent asking about a node was told
    its type and label while a browser was given everything the graph holds
    about it. Nobody decided that; the key was added on one surface.
    """

    attrs = context.graph.nodes.get(node_id)
    if attrs is None:
        return {"node_id": node_id, "explanation": "Node is not present in the current graph."}
    node = dict(attrs)
    return {
        "node_id": node_id,
        "type": node.get("type"),
        "label": node.get("label"),
        "explanation": f"{node.get('label')} is a {node.get('type')} node indexed by pheasant.",
        "provenance": node.get("provenance"),
        "node": node,
    }


def require_readable(
    context: ServiceContext,
    artifact_id: str | None,
    principal: str | None,
    principal_groups: list[str] | None = None,
) -> None:
    """Refuse unless ``principal`` may read ``artifact_id`` (Step 32.2).

    The content operations hand back whole artifact bodies by id or path, which
    bypasses the filtering ``search_context`` does — an ACL-enforcing region
    that filters search results and then serves the same bytes from a summary
    endpoint has not enforced anything. A no-op when ``security.acl_enforced``
    is off, so a region that never turned it on is unchanged.

    It lived on the HTTP surface only. `get_file_summary` over MCP therefore
    returned the artifact row *and*, after this module unified the query, its
    full text to any caller — on the surface whose callers are agents. That is
    the sharpest example of what two implementations of one operation cost:
    the fix landed where the review came from, and the other copy kept the bug.
    """

    security = context.config.security
    if not security.acl_enforced:
        return
    from pheasant.security.acl import is_allowed

    identities = _identities(context, principal, principal_groups)
    default_public = security.default_visibility != "private"
    acls = context.state.artifact_acls([artifact_id]) if artifact_id else {}
    if artifact_id not in acls:
        # Not resolvable to an artifact row: deny, matching the conservative
        # rule the search path applies to bare graph nodes.
        raise NotPermitted
    if not is_allowed(acls[artifact_id], identities, default_public=default_public):
        raise NotPermitted


def _identities(
    context: ServiceContext, principal: str | None, principal_groups: list[str] | None
) -> Any:
    """Who ``principal`` is, for an ACL check: their groups plus fresh IdP ones.

    The same expansion the search path performs, so a node is visible here
    exactly when the artifact it came from would be visible as a hit.
    """

    from pheasant.security.acl import expand_principal

    security = context.config.security
    identities = expand_principal(principal, principal_groups, security.groups)
    if identities is not None and principal:
        from pheasant.security.idp import fresh_idp_groups

        identities |= fresh_idp_groups(context.state, principal, security.idp)
    return identities


def neighbor_filter(
    context: ServiceContext,
    principal: str | None,
    principal_groups: list[str] | None = None,
) -> Any:
    """A filter over walk neighbours for ``principal``, or None if ACLs are off.

    A graph walk returns node attributes, and a chunk node carries the opening
    of its text, so expanding a hit the caller may read must not surface a
    neighbour they may not. A node is judged by the artifact it *is*, or else
    the artifact it names as ``artifact_id`` (chunks, symbols, entities); a
    node that resolves to neither (a directory, a source, a shared stub) is
    withheld, which is the conservative rule the search path already applies
    to graph hits with no artifact row.

    ``None`` when ``security.acl_enforced`` is off, so a region that never
    turned enforcement on pays nothing for it.
    """

    security = context.config.security
    if not security.acl_enforced:
        return None
    from pheasant.security.acl import is_allowed

    identities = _identities(context, principal, principal_groups)
    default_public = security.default_visibility != "private"

    def owner(item: dict[str, Any]) -> str:
        node = item.get("node") or {}
        return str(node.get("artifact_id") or item["node_id"])

    def admit(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = {str(item["node_id"]) for item in found} | {owner(item) for item in found}
        acls = context.state.artifact_acls(sorted(candidates))
        kept = []
        for item in found:
            node_id = str(item["node_id"])
            key = node_id if node_id in acls else owner(item)
            if key in acls and is_allowed(acls[key], identities, default_public=default_public):
                kept.append(item)
        return kept

    return admit


def file_summary(
    context: ServiceContext,
    path: str,
    source_name: str | None = None,
    principal: str | None = None,
    principal_groups: list[str] | None = None,
) -> dict[str, Any]:
    """One indexed file: its artifact row, its summary, and its text.

    ``GROUP_CONCAT`` order is arbitrary unless the input rows are ordered, so
    the summary and content are concatenated over ordered scalar subqueries.
    The MCP copy used a bare ``GROUP_CONCAT`` — its summaries came back in
    whatever order the join produced — and returned no content at all.
    """

    rows = context.state.rows(
        """SELECT artifacts.*,
        (SELECT GROUP_CONCAT(summary, '\n') FROM
            (SELECT summary FROM chunks WHERE artifact_id=artifacts.id
             ORDER BY chunk_index)) AS summary,
        (SELECT GROUP_CONCAT(text, '\n\n') FROM
            (SELECT text FROM chunks WHERE artifact_id=artifacts.id
             ORDER BY chunk_index)) AS content
        FROM artifacts
        WHERE artifacts.relative_path=? AND (? IS NULL OR artifacts.source_id=?)
        LIMIT 1""",
        (path, source_name, source_name),
    )
    if not rows:
        return {"path": path, "summary": None}
    require_readable(context, str(rows[0]["id"]), principal, principal_groups)
    return dict(rows[0])


def repo_map(context: ServiceContext, source_name: str, depth: int = 3) -> dict[str, Any]:
    """Every indexed path in one source.

    ``depth`` is accepted and unused, as it was on both surfaces before this:
    the map is flat and the argument is part of the MCP tool's published
    signature, which rule 8 makes additive-only. Documented rather than
    quietly dropped.
    """

    rows = context.state.rows(
        "SELECT relative_path,type,size_bytes FROM artifacts "
        "WHERE source_id=? ORDER BY relative_path",
        (source_name,),
    )
    return {"source_name": source_name, "files": [dict(row) for row in rows]}
