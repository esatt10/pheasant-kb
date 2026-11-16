"""Refuse new content when the knowledge base is at its size limit.

The limit and its measure are ``persistence/size_limit.py``; a sync that would
add content is skipped there, in the engine, which every sync passes. This is
the other half: the operations that hand the region content directly say so at
once, with what to do about it, instead of accepting bytes no sync will index.
"""

from __future__ import annotations

from pheasant.persistence import size_limit
from pheasant.services import ServiceContext
from pheasant.services.errors import KnowledgeBaseFull


def require_room(context: ServiceContext) -> None:
    full = size_limit.reached(context.config, context.state)
    if full is not None:
        raise KnowledgeBaseFull(full["indexed_bytes"], full["limit_gb"])
