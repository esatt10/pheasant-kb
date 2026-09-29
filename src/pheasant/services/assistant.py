"""The assistant's retrieval vocabulary, shared by both surfaces.

One line per retrieval knob, so a UI — or an agent reading
``describe_retrieval`` — can explain a setting without going and reading the
workflow module's docstring.

It lived in `api/app.py`, and `mcp_server/tools.py` imported it from there. The
text is not HTTP's; it is the operation's, and a tool layer importing a
transport module for a constant is the shape this package exists to remove.
"""

from __future__ import annotations

#: One line per retrieval knob, so a UI (or an agent reading
#: ``describe_retrieval``) can explain a setting without the caller having to
#: go and read the workflow module's docstring.
RETRIEVAL_FIELD_HELP: dict[str, str] = {
    "max_rounds": "plan → retrieve → grade turns before answering with what is in "
    "hand. 1 disables the re-plan loop.",
    "per_query_results": "passages fetched per query per search mode.",
    "max_context_passages": "total passages offered to the answering step.",
    "retrieval_modes": "search modes to fan out over (text, vector, graph, hybrid). "
    "'vector' is dropped automatically when no vector index is built.",
    "expand_graph": "walk the knowledge graph out of the best hits, reaching "
    "documents that share no vocabulary with the question.",
    "expand_depth": "hops to walk when expanding.",
    "expand_per_node": "neighbours taken per expanded node.",
    "grade_evidence": "ask the model to grade its own evidence before answering.",
    "grader_model": "optional model for evidence sufficiency checks; the assistant model "
    "still writes the answer.",
    "verify_citations": "drop [n] markers that do not resolve to a real citation.",
    "max_facts": "graph facts surfaced alongside the answer.",
}
