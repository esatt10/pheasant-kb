"""An installed extension's chat keyword (``@harness``), as the asker.

The answer service hands the answering pipeline one callable for the
keyword, the way ``@remove`` is handed ``services.archive``, so the pipeline
knows no extension. Kept apart from ``services.assistant`` because resolving an
extension is not part of answering a question.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pheasant.services import ServiceContext


def commander(context: ServiceContext, request: Any) -> Callable[[str, str], dict]:
    """An extension's chat keyword (``@harness``): its operation, as the asker.

    Resolved here, in the service layer, so the answering pipeline knows no
    extension: it is handed one callable, the way ``@remove`` is handed
    ``services.archive``. The extension runs under the identity the transport
    bound, so its own authorization applies exactly as on its HTTP routes.
    """

    from pheasant.extensions import ChatCommandRequest, extension_chat_commands

    def run(keyword: str, text: str) -> dict[str, Any]:
        found = extension_chat_commands().get(keyword)
        if found is None:
            raise ValueError(f"no installed extension answers @{keyword}")
        extension_id, command = found
        result = dict(
            command.run(
                context,
                ChatCommandRequest(
                    text=text,
                    keyword=keyword,
                    principal=request.principal,
                    principal_groups=tuple(request.principal_groups or ()),
                    knowledge_base=request.knowledge_base,
                    conversation_id=request.conversation_id,
                    channel=request.channel,
                    source_name=request.source_name,
                    source_names=tuple(request.source_names or ()),
                    current_document=request.current_document,
                    current_directory=request.current_directory,
                ),
            )
            or {}
        )
        return {"extension": extension_id, **result}

    return run
