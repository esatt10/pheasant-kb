"""The region over MCP: whether it is awake (``get_region_status``) and the
operator's controls over it (``get_region_controls`` / ``set_region_controls``).

A mixin plus a registration function, like ``sync_pipeline_tools.py``.
Adapters over `services.region_status`, which ``GET /region/status`` and the
web UI's banner use too, and `services.region_controls`, behind ``GET``/``PUT
/region/controls``.
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

    def get_region_controls(self, knowledge_base: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import region_controls

        return region_controls.describe(self.services)

    def set_region_controls(
        self,
        knowledge_base: str,
        suspended: bool | None = None,
        reason: str | None = None,
        monthly_spend_limit: float | str | None = None,
        deep_answer_spend_limit: float | str | None = None,
        max_concurrent_deep_answers: int | str | None = None,
        max_concurrent_answers: int | str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        from pheasant.services import region_controls

        changes: dict[str, Any] = {}
        if suspended is not None:
            changes["suspended"] = suspended
        if reason is not None:
            changes["reason"] = reason
        # "default" is MCP's way to send null: an omitted argument and an
        # explicit null arrive the same here.
        for name, value in (
            ("monthly_spend_limit", monthly_spend_limit),
            ("deep_answer_spend_limit", deep_answer_spend_limit),
            ("max_concurrent_deep_answers", max_concurrent_deep_answers),
            ("max_concurrent_answers", max_concurrent_answers),
        ):
            if value is not None:
                default = str(value).strip().lower() == "default"
                changes[name] = None if default else value
        return region_controls.update(self.services, changes)


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

    @mcp.tool()
    @anticipated
    def get_region_controls(knowledge_base: str) -> dict:
        """The operator's controls over this knowledge base (Admin only).

        ``access.suspended`` is the kill switch: while it is on, every request
        is refused with ACCESS_SUSPENDED except an operator admin's.
        ``spend`` is this calendar month's (UTC) chat, judgement and embedding
        spend at the catalog's stated prices against ``limit`` (default 500 a
        month); at the limit (``reached``) model calls are refused with
        SPEND_LIMIT_REACHED until ``resets_on`` or until the limit is raised,
        while text search, browsing and reading keep working. The amounts are
        shown to an operator admin only; others see ``percent_used``.
        ``deep_research`` is what one deep-research answer (@detailed / @deep)
        may spend (``answer_spend_limit``, default 5; research wraps up at
        ``wrap_up_percent``) and how many run at once per api process
        (``max_concurrent``, default 2).
        ``answers`` is how many answers of any kind one api process runs at once
        (``max_concurrent``) and how long the next one waits for a slot before
        ASSISTANT_BUSY (``wait_seconds``).
        ``can_change`` says whether the caller may call set_region_controls.
        """

        return tools.get_region_controls(knowledge_base)

    @mcp.tool()
    @anticipated
    def set_region_controls(
        knowledge_base: str,
        suspended: bool | None = None,
        reason: str | None = None,
        monthly_spend_limit: float | str | None = None,
        deep_answer_spend_limit: float | str | None = None,
        max_concurrent_deep_answers: int | str | None = None,
        max_concurrent_answers: int | str | None = None,
    ) -> dict:
        """Suspend or restore access, or set the spend limits (operator admin only).

        ``suspended`` true cuts access for everyone but an operator admin at
        once (within seconds on every replica); false restores it. ``reason``
        is shown to everyone refused. ``monthly_spend_limit`` is the month's
        limit in the catalog's currency (0 or more), or "default" for the
        deployment's. ``deep_answer_spend_limit`` is what one deep-research
        answer may spend (0 or more, or "default"); research wraps up at 95% of
        it. ``max_concurrent_deep_answers`` is how many deep answers one api
        process runs at once (0 for no separate cap, or "default"); past it a
        caller gets DEEP_RESEARCH_BUSY, retryable. ``max_concurrent_answers`` is
        how many answers of any kind one api process runs at once (1 to 256, or
        "default"); past it a question waits briefly for a slot, then gets
        ASSISTANT_BUSY, retryable. Only an operator admin (an
        Admin the deployment pinned, or the shared token) may call it:
        OPERATOR_REQUIRED otherwise. Returns get_region_controls.
        """

        return tools.set_region_controls(
            knowledge_base,
            suspended,
            reason,
            monthly_spend_limit,
            deep_answer_spend_limit,
            max_concurrent_deep_answers,
            max_concurrent_answers,
        )
