"""Version 1 of the installed Pheasant extension contract."""

from __future__ import annotations

import re
from argparse import ArgumentParser, Namespace
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from pheasant.config.schema import SourceConfig
    from pheasant.persistence.state_store import StateStore
    from pheasant.sync.connectors import SourceConnector

API_VERSION = 1
ENTRY_POINT = "pheasant.extensions"


@dataclass(frozen=True)
class Provenance:
    node_type: str
    id_prefix: str

    def node_id(self, artifact_id: str, reference: str) -> str:
        return f"{self.id_prefix}:{artifact_id}:{reference}"


@dataclass(frozen=True)
class SourceContribution:
    type: str
    label: str
    description: str
    connector: Callable[[SourceConfig, StateStore], SourceConnector]
    registration_help: str
    legacy_settings_key: str | None = None
    provenance: Provenance | None = None
    public_sync_details: tuple[str, ...] = ()


@dataclass(frozen=True)
class Command:
    name: str
    help: str
    configure: Callable[[ArgumentParser], None]
    run: Callable[[Namespace], int]


@dataclass(frozen=True)
class ChatCommand:
    """A first-word chat keyword an extension answers (``@harness …``).

    The chat reads it like ``@remove``: only as the message's first word, with
    no search, no history rewrite and no model call on Pheasant's side. ``run``
    is called with the region's service context and a
    :class:`ChatCommandRequest`, and returns a mapping with ``text`` (Markdown,
    the answer shown to the reader) and anything else the extension wants the
    caller to have, carried on the answer as ``extension``. A refusal is
    answered, not raised: an extension raising ``ValueError`` has its message
    shown as the answer.
    """

    name: str
    summary: str
    example: str
    run: Callable[[Any, ChatCommandRequest], Mapping[str, Any]]
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChatCommandRequest:
    """What the chat knows when a command is asked: the words after it and where."""

    text: str
    keyword: str
    principal: str | None = None
    principal_groups: tuple[str, ...] = ()
    knowledge_base: str | None = None
    conversation_id: str | None = None
    channel: str | None = None
    source_name: str | None = None
    source_names: tuple[str, ...] = ()
    current_document: str | None = None
    current_directory: str | None = None


@dataclass(frozen=True)
class Extension:
    id: str
    api_version: int
    register_http: Callable[[FastAPI, Any], None] | None = None
    http_permissions: Mapping[tuple[str, str], str] = field(default_factory=dict)
    register_mcp: Callable[[Any, Any, Any], None] | None = None
    mcp_permissions: Mapping[str, str] = field(default_factory=dict)
    sources: tuple[SourceContribution, ...] = ()
    commands: tuple[Command, ...] = ()
    # Each immutable tuple is one transactional migration, numbered from one.
    migrations: tuple[tuple[str, ...], ...] = ()
    ui_routes: tuple[str, ...] = ()
    chat_commands: tuple[ChatCommand, ...] = ()


@cache
def installed_extensions() -> tuple[Extension, ...]:
    """Load trusted, explicitly installed distributions before serving traffic."""
    entries = sorted(entry_points(group=ENTRY_POINT), key=lambda entry: entry.name)
    found = []
    for entry in entries:
        extension = entry.load()()
        if (
            not isinstance(extension, Extension)
            or extension.id != entry.name
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", extension.id)
            or extension.api_version != API_VERSION
        ):
            raise RuntimeError(f"Invalid or incompatible Pheasant extension: {entry.name}")
        if any(not re.fullmatch(r"/[a-z][a-z0-9-]*", path) for path in extension.ui_routes):
            raise RuntimeError(f"Invalid UI route declared by extension {extension.id}")
        if any(
            not re.fullmatch(r"[a-z][a-z0-9-]{1,30}", name)
            for command in extension.chat_commands
            for name in (command.name, *command.aliases)
        ):
            raise RuntimeError(f"Invalid chat keyword declared by extension {extension.id}")
        found.append(extension)
    _unique((extension.id for extension in found), "ID")
    _unique((source.type for extension in found for source in extension.sources), "source type")
    _unique((command.name for extension in found for command in extension.commands), "command")
    _unique((path for extension in found for path in extension.ui_routes), "UI route")
    _unique(
        (
            name
            for extension in found
            for command in extension.chat_commands
            for name in (command.name, *command.aliases)
        ),
        "chat keyword",
    )
    return tuple(found)


def _unique(values, label: str) -> None:
    seen = set()
    for value in values:
        if value in seen:
            raise RuntimeError(f"Duplicate Pheasant extension {label}: {value}")
        seen.add(value)


@cache
def extension_sources() -> dict[str, SourceContribution]:
    from pheasant.config.schema import SourceType

    sources = {
        source.type: source for extension in installed_extensions() for source in extension.sources
    }
    collision = sources.keys() & {kind.value for kind in SourceType}
    if collision:
        raise RuntimeError(f"Extensions cannot replace built-in sources: {sorted(collision)}")
    return sources


@cache
def extension_chat_commands() -> dict[str, tuple[str, ChatCommand]]:
    """Every extension chat keyword and alias -> (extension id, command)."""
    return {
        name: (extension.id, command)
        for extension in installed_extensions()
        for command in extension.chat_commands
        for name in (command.name, *command.aliases)
    }


@cache
def extension_ui_routes() -> frozenset[str]:
    return frozenset(path for extension in installed_extensions() for path in extension.ui_routes)


@cache
def extension_provenance_types() -> frozenset[str]:
    return frozenset(
        source.provenance.node_type for source in extension_sources().values() if source.provenance
    )


def source_settings(data: dict, source: SourceContribution) -> dict | None:
    """Read the current settings envelope or an extension's earlier wire format."""
    if data.get("extension_settings") is not None:
        return data["extension_settings"]
    return data.get(source.legacy_settings_key) if source.legacy_settings_key else None


def extension_permissions(surface: str, core: Mapping) -> dict:
    from pheasant.security.authorization import ROLE_PERMISSIONS

    valid = frozenset().union(*ROLE_PERMISSIONS.values())
    result = dict(core)
    for extension in installed_extensions():
        for address, permission in getattr(extension, f"{surface}_permissions").items():
            if address in result or permission not in valid:
                raise RuntimeError(f"Invalid {surface} permission in {extension.id}: {address}")
            result[address] = permission
    return result
