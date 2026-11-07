"""How much of a source the index can read, as a percentage, and what to call it.

A sync that could not read some items used to call the whole source
``degraded``: 4 dead links among 17,059 pages read exactly like 77 refusals
among 80, so the word stopped saying anything. The score is the share that
reads -- documents the index holds over those plus the items its last sync
could not read -- and the status follows the majority: ``degraded`` only when
at most half of what the source lists is readable.

The score is computed when it is read, not only when a sync ends, so taking an
item out changes it at once: a URL removed from a web collection, or a document
archived, stops counting against the source before the sync that removes it
has run. The last sync records every unreadable item (path and URL) for that
reason; an event written before that kept the first 25, and the rest still
count until the next sync records them all.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

#: The share of a source that must read for it to be called healthy. At or
#: below it, most of what the source lists is not in the index.
DEGRADED_AT_OR_BELOW_PERCENT = 50.0

#: The unreadable items one sync event records in full. Past this the count is
#: still exact; only the items beyond it cannot be taken out by a removal.
MAX_RECORDED_UNREADABLE = 10_000

#: The statuses a score speaks for. Anything else (``failed``, ``registered``,
#: ``disabled``, ``syncing``) is about the sync, not about what it read.
SCORED_STATUSES = frozenset({"healthy", "degraded"})

#: The source types whose items are a URL list, so a removed URL is a dropped item.
URL_SOURCE_TYPES = frozenset({"web_collection", "api"})

#: Below this many tokens of extracted text a file is flagged ``little_text``:
#: a heading and a sentence, or an error page that answered 200.
LOW_TEXT_TOKENS = 20

#: An unreadable item's error, as recorded for every item (the first 25 keep
#: theirs whole in ``unreadable``).
MAX_ERROR_CHARS = 300

SAMPLE = 5
_BATCH = 500


@dataclass(frozen=True)
class Failures:
    """The last sync's unreadable items that still count: path -> (url, error)."""

    items: dict[str, tuple[str | None, str | None]]
    dropped: int
    unrecorded: int
    as_of: str | None


def percent(readable: int, unreadable: int) -> float | None:
    """``readable`` as a share of both, rounded *down* to one decimal.

    Down, so a source with anything unreadable never shows 100%. ``None`` when
    there is nothing to score.
    """

    total = max(0, readable) + max(0, unreadable)
    if total == 0:
        return None
    return math.floor(max(0, readable) * 1000 / total) / 10


def status_for(score: float | None) -> str:
    """``healthy`` unless at most half of the source reads."""

    if score is None or score > DEGRADED_AT_OR_BELOW_PERCENT:
        return "healthy"
    return "degraded"


def current_failures(state: Any, source: dict[str, Any]) -> Failures | None:
    """What the source's last sync could not read and still counts, as of now.

    ``None`` when no sync has finished, or the last one did not finish reading
    (a failed or cancelled pass says nothing about what is readable). A URL
    since removed from the source, or a document since archived, is
    ``dropped``: it no longer counts against the source or the file.
    """

    from pheasant.persistence import archive

    name = str(source["name"])
    events = state.list_sync_events(name, limit=1)
    if not events or events[0].get("status") not in SCORED_STATUSES:
        return None
    event = events[0]
    details = event.get("details") or {}
    failures = _recorded_failures(details)
    counted = int(details.get("unreadable_items") or len(failures))
    held = {str(row["relative_path"]) for row in archive.for_source(state, name)}
    urls = _listed_urls(source)
    still = {
        path: (uri, error)
        for path, uri, error in failures
        if path not in held and (urls is None or not uri or uri in urls)
    }
    return Failures(
        items=still,
        dropped=len(failures) - len(still),
        unrecorded=max(0, counted - len(failures)),
        as_of=event.get("finished_at"),
    )


def source_health(state: Any, source: dict[str, Any]) -> dict[str, Any] | None:
    """The score for one registered source row, from its last sync and what it holds now."""

    failures = current_failures(state, source)
    if failures is None:
        return None
    name = str(source["name"])
    documents = _count(state, "SELECT COUNT(*) AS n FROM artifacts WHERE source_id=?", (name,))
    # A page that read once and fails now keeps its old text in the index:
    # it is held, and it is not readable.
    stale = _held(state, name, list(failures.items))
    readable = documents - stale
    unreadable = len(failures.items) + failures.unrecorded
    score = percent(readable, unreadable)
    return {
        "percent": score,
        "status": status_for(score),
        "readable": readable,
        "unreadable": unreadable,
        "dropped": failures.dropped,
        "as_of": failures.as_of,
        "unreadable_sample": [
            {"path": path, "error": error}
            for path, (_, error) in list(failures.items.items())[:SAMPLE]
        ],
    }


# -- one file -----------------------------------------------------------------


def file_quality(
    *,
    held: bool,
    chunks: int = 0,
    tokens: int = 0,
    read_error: str | None = None,
    script_shell: bool = False,
) -> dict[str, Any]:
    """One file's state: ``ok``, ``warning`` or ``error``, and every flag with its reason.

    ``held`` is whether the index has text for it at all. Each rule reads
    something the region already records -- the last sync's failures, the
    connector's checkpoint, the chunk rows -- so the answer is the same on any
    replica and moves when the file does. Deterministic, like everything else
    on the indexing side: no model judges a file.
    """

    flags: list[dict[str, str]] = []
    if read_error is not None:
        if held:
            reason = (
                f"the last sync could not read it ({read_error}); the text served is "
                "from an earlier read"
            )
            flags.append(_flag("stale", "warning", reason))
        else:
            reason = (
                f"the last sync could not read it ({read_error}), and the index holds "
                "nothing for it"
            )
            flags.append(_flag("unreadable", "error", reason))
    if held:
        if script_shell:
            reason = (
                "its server sent a JavaScript application shell rather than page content; "
                "pheasant does not run page scripts, so the shell is what was indexed"
            )
            flags.append(_flag("script_shell", "warning", reason))
        if chunks == 0:
            reason = (
                "no text was extracted from it: a scanned PDF, an image with no caption "
                "or an empty file reads this way"
            )
            flags.append(_flag("no_text", "warning", reason))
        elif tokens < LOW_TEXT_TOKENS:
            reason = f"only about {tokens} tokens of text were extracted from it"
            flags.append(_flag("little_text", "warning", reason))
    severities = {flag["severity"] for flag in flags}
    status = "error" if "error" in severities else "warning" if flags else "ok"
    return {"status": status, "flags": flags}


def script_shells(state: Any, name: str) -> set[str]:
    """The URLs whose server sent only a JavaScript shell, per the connector's checkpoint."""

    checkpoint = state.get_source_checkpoint(name) or {}
    shells: set[str] = set()
    for part in ("cursor", "high_watermark"):
        shells.update(str(url) for url in (checkpoint.get(part) or {}).get("script_shells") or [])
    return shells


def _flag(code: str, severity: str, reason: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "reason": reason}


def scored_status(status: Any, health: dict[str, Any] | None) -> Any:
    """The status a reader should see: the score's, where the score speaks for it."""

    if health is not None and status in SCORED_STATUSES:
        return health["status"]
    return status


def _recorded_failures(details: dict[str, Any]) -> list[tuple[str, str | None, str | None]]:
    errors = {str(item["path"]): item.get("error") for item in details.get("unreadable") or []}
    recorded = details.get("unreadable_all")
    if recorded is not None:
        return [
            (str(entry[0]), entry[1], entry[2] if len(entry) > 2 else errors.get(str(entry[0])))
            for entry in recorded
        ]
    # An event from before every item was recorded: its sample, without URLs.
    return [(path, None, error) for path, error in errors.items()]


def _listed_urls(source: dict[str, Any]) -> set[str] | None:
    if str(source.get("type")) not in URL_SOURCE_TYPES:
        return None
    raw = source.get("config_json")
    config = json.loads(raw) if isinstance(raw, str) and raw else source.get("config") or {}
    return set(config.get("urls") or [])


def _count(state: Any, sql: str, params: tuple) -> int:
    rows = state.rows(sql, params)
    return int(rows[0]["n"] or 0) if rows else 0


def _held(state: Any, name: str, paths: list[str]) -> int:
    held = 0
    for start in range(0, len(paths), _BATCH):
        batch = paths[start : start + _BATCH]
        marks = ",".join("?" for _ in batch)
        held += _count(
            state,
            f"SELECT COUNT(*) AS n FROM artifacts WHERE source_id=? AND relative_path IN ({marks})",
            (name, *batch),
        )
    return held
