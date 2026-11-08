"""Whether the region is awake, over MCP: ``get_region_status``.

A mixin plus a registration function, like ``sync_pipeline_tools.py``. An
adapter over `services.region_status`, which ``GET /region/status`` and the
web UI's banner use too.
"""

from __future__ import annotations

from typing import Any


class RegionTools:
    """``services.region_status`` adapter. Parse, call, return."""

    services: Any

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def get_region_status(self, knowledge_base: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import region_status

        return region_status.region_status(self.services)


def register_region_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    @mcp.tool()
    @anticipated
    def get_region_status(knowledge_base: str) -> dict:
        """Say whether the region is fully awake, and if not, what is still starting.

        A region that scales to zero wakes on its first request, and its parts
        come up at different speeds. ``status`` is ``ready`` (nothing to wait
        for), ``waking`` (a part is still starting; calls that need it may
        fail, so wait and retry), ``warming`` (everything answers, but the
        database is still reloading its cache, so the first answers may be
        slower) or ``unavailable``. ``components`` gives each part's state and
        a sentence; ``elapsed_seconds`` is how long this wake has taken so far
        and ``typical_seconds`` how long one usually takes. Call it when a
        tool times out or reports something unreachable.
        """

        return tools.get_region_status(knowledge_base)
