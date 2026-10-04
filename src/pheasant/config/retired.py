"""Source types that existed once and no longer do.

Configs and the runtime source registry in ``/state`` are user data (CLAUDE.md
rule 2), so a removed type must neither fail a config load nor vanish without a
word. Two treatments, by whether a replacement exists:

* an **alias** is rewritten to its replacement at load time, with a one-time
  notice. ``obsidian_vault`` was a filesystem walk with Markdown chunking;
  wiki links and ``![[embeds]]`` are resolved from file content, not from the
  source type, so a vault indexed as ``markdown_folder`` keeps both.
* a **removed** type still loads (as a plugin type nothing provides) and is
  refused at sync with the reason, which is what ``connector_for_source``
  reports instead of "no connector registered".
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SOURCE_TYPE_ALIASES: dict[str, str] = {
    "obsidian_vault": "markdown_folder",
}

REMOVED_SOURCE_TYPES: dict[str, str] = {
    "s3": "the S3 connector was removed; sync the bucket to a folder and index that.",
    "notion": "the Notion connector was removed; export pages to Markdown and index the folder.",
    "slack": "the Slack connector was removed.",
    "confluence": "the Confluence connector was removed; export the space and index it.",
    "imap": "the IMAP connector was removed.",
}

_WARNED: set[str] = set()


def resolve_source_type(type_name: str) -> str:
    """The current name for ``type_name``, logging once if it was renamed."""

    replacement = SOURCE_TYPE_ALIASES.get(type_name)
    if replacement is None:
        return type_name
    if type_name not in _WARNED:
        _WARNED.add(type_name)
        logger.warning(
            "source type %r was retired; loading it as %r. Update the config to say %r.",
            type_name,
            replacement,
            replacement,
        )
    return replacement
