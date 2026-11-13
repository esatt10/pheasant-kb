"""The artifact settings a running region is using, changed for every replica at once.

``config.artifacts`` holds the defaults; one ``region_settings`` row laid over
them holds what somebody changed from the Artifacts panel, ``PUT
/artifacts/settings`` or MCP ``configure_artifacts``. A config file edited on
one replica reaches that replica; a row in the state store reaches all of them
on their next read -- the same reason the tuning plane's bundle is a row.

Read per use (one primary-key read), never cached: whether an answer is kept
is decided once per answer, and a toggle that took a TTL to apply would keep
the one answer somebody turned it off to avoid.
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import UTC, datetime
from typing import Any

from pheasant.config.schema import ARTIFACT_KINDS, ArtifactsSettings
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest

SECTION = "artifacts"
_TYPES = {f.name: f.type for f in fields(ArtifactsSettings) if f.name != "learning"}


def _defaults(context: ServiceContext) -> dict[str, Any]:
    configured = getattr(context.config, "artifacts", None) or ArtifactsSettings()
    return {k: v for k, v in asdict(configured).items() if k in _TYPES}


def _overlay(context: ServiceContext) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = context.state.rows(
        "SELECT data_json, updated_at, updated_by FROM region_settings WHERE kb_id=? AND section=?",
        (context.config.knowledge_base_id, SECTION),
    )
    if not rows:
        return {}, {}
    row = dict(rows[0])
    data = json.loads(row.get("data_json") or "{}")
    return (
        {k: v for k, v in data.items() if k in _TYPES},
        {"updated_at": row.get("updated_at"), "updated_by": row.get("updated_by")},
    )


def current(context: ServiceContext) -> ArtifactsSettings:
    """The settings in force: the config's defaults with the region's changes on top."""

    overlay, _meta = _overlay(context)
    return ArtifactsSettings(**{**_defaults(context), **overlay})


def describe(context: ServiceContext) -> dict[str, Any]:
    """The settings in force, what the config says, and who changed what."""

    overlay, meta = _overlay(context)
    return {
        "settings": asdict(current(context)),
        "defaults": _defaults(context),
        "changed": sorted(overlay),
        **meta,
    }


def _coerce(name: str, value: Any) -> Any:
    kind = str(_TYPES[name])
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in {"true", "on", "1", "yes"}:
            return True
        if str(value).strip().lower() in {"false", "off", "0", "no"}:
            return False
        raise InvalidRequest(f"artifacts.{name} must be true or false; got {value!r}")
    if kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise InvalidRequest(f"artifacts.{name} must be a whole number; got {value!r}") from exc
        if number < 0:
            raise InvalidRequest(f"artifacts.{name} cannot be negative")
        return number
    kinds = [str(k).strip().lower() for k in (value or [])]
    unknown = sorted(set(kinds) - set(ARTIFACT_KINDS))
    if isinstance(value, str) or unknown:
        raise InvalidRequest(
            f"artifacts.{name} is a list of {', '.join(ARTIFACT_KINDS)}; got {value!r}"
        )
    return kinds


def update(context: ServiceContext, changes: dict[str, Any], actor: str | None) -> dict[str, Any]:
    """Change some settings for every replica; the settings now in force.

    A key set back to its config default is dropped from the overlay, so
    "what did somebody change" stays answerable.
    """

    if not isinstance(changes, dict) or not changes:
        raise InvalidRequest("name at least one artifacts setting to change")
    unknown = sorted(set(changes) - set(_TYPES))
    if unknown:
        raise InvalidRequest(
            f"unknown artifacts setting(s): {', '.join(unknown)}; known: {', '.join(_TYPES)}"
        )
    overlay, _meta = _overlay(context)
    defaults = _defaults(context)
    for name, value in changes.items():
        overlay[name] = _coerce(name, value)
        if overlay[name] == defaults.get(name):
            overlay.pop(name)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    context.state.execute(
        "INSERT INTO region_settings(kb_id, section, data_json, updated_at, updated_by) "
        "VALUES(?,?,?,?,?) ON CONFLICT(kb_id, section) DO UPDATE SET "
        "data_json=excluded.data_json, updated_at=excluded.updated_at, "
        "updated_by=excluded.updated_by",
        (context.config.knowledge_base_id, SECTION, json.dumps(overlay), now, actor),
    )
    if {"index_interval_seconds", "yield_to_content"} & set(changes):
        from pheasant.services import artifacts

        artifacts.apply_pacing(context)
    if "taxonomy_enabled" in changes:
        apply_taxonomy(context)
    return describe(context)


def apply_taxonomy(context: ServiceContext) -> None:
    from pheasant.config.schema import SourceConfig
    from pheasant.registry.source_registry import SourceRegistry

    registry = SourceRegistry(context.config, context.state)
    configured = {source.name: source for source in context.config.sources}
    for row in context.state.rows(
        "SELECT config_json FROM sources WHERE type IN ('artifact','memory') OR id='artifacts'"
    ):
        payload = json.loads(row["config_json"])
        source = configured.get(payload["name"]) or SourceConfig.model_validate(payload)
        source.taxonomy.enabled = current(context).taxonomy_enabled
        registry.register_source(source, revive=False)
