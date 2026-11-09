"""The ``artifacts`` switch on search and answers: include, exclude, or only.

Like ``memory`` for memory records, and applied the same way on every
surface (`services.retrieval.search`, `services.assistant.answer`).
"""

from __future__ import annotations

from typing import Any

from pheasant.services.errors import InvalidRequest

#: The source every artifact lands in.
ARTIFACTS_SOURCE = "artifacts"
#: How a search or an answer treats saved artifacts, like ``memory`` does for
#: memory records: ``include`` (the default: they are knowledge like any other
#: document), ``exclude`` (the corpus as it was before anything was composed
#: from it) or ``only`` (search what has been composed).
ARTIFACT_MODES = ("include", "exclude", "only")


def artifacts_mode(value: Any) -> str:
    """``include`` / ``exclude`` / ``only`` from what a caller sent, or a refusal.

    A boolean reads as the memory toggle does: ``true`` includes, ``false``
    leaves them out.
    """

    if value is None or value == "" or value is True:
        return "include"
    if value is False:
        return "exclude"
    mode = str(value).strip().lower()
    if mode in ARTIFACT_MODES:
        return mode
    raise InvalidRequest(
        f"artifacts must be one of {', '.join(ARTIFACT_MODES)} (or true/false); got {value!r}"
    )


def scope(
    value: Any,
    source_name: str | None,
    source_names: list[str] | None,
    exclude_sources: list[str] | None,
) -> tuple[str | None, list[str] | None, list[str] | None]:
    """``(source_name, source_names, exclude_sources)`` with the artifacts toggle applied.

    ``exclude`` is an ordinary source exclusion and ``only`` the ordinary
    single-source scope, so every arm, filter and over-fetch already honours
    it. ``only`` beside a scope naming other sources is refused rather than
    silently overriding it.
    """

    mode = artifacts_mode(value)
    if mode == "exclude":
        excluded = list(dict.fromkeys([*(exclude_sources or []), ARTIFACTS_SOURCE]))
        return source_name, source_names, excluded
    if mode == "only":
        named = {n for n in [source_name, *(source_names or [])] if n} - {ARTIFACTS_SOURCE}
        if named:
            raise InvalidRequest(
                "artifacts: only searches the artifacts source and cannot be combined "
                f"with a scope naming {', '.join(sorted(named))}"
            )
        return ARTIFACTS_SOURCE, None, exclude_sources
    return source_name, source_names, exclude_sources
