"""A web collection's pages, over HTTP: add some, take some out.

One router per plane, as `api/app.py`'s ceiling comment asks. The handler
parses the request and calls `services.source_urls`; MCP's
``edit_source_urls`` calls the same function. How this process reaches the
index (here, or by publishing to an indexer) rides in the service context's
``indexing``.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from pheasant.services import source_urls


class EditSourceUrlsRequest(BaseModel):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    #: Sync the source afterwards (in the background, or published to an
    #: indexer), which is what fetches an added page and drops a removed one.
    sync_now: bool = True


def register_source_url_routes(app: FastAPI, *, services: Any) -> None:
    @app.post("/sources/{source_name}/urls")
    def edit_source_urls(source_name: str, req: EditSourceUrlsRequest) -> dict:
        """Add URLs to a web collection or remove them, keeping every other page.

        ``urls`` is the list afterwards; ``added``/``removed`` what changed, and
        ``already_listed``/``not_listed`` what did not need to."""

        return source_urls.edit_urls(
            services,
            source_urls.UrlEditRequest(
                source_name=source_name,
                add=list(req.add),
                remove=list(req.remove),
                sync=req.sync_now,
            ),
        )
