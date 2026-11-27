"""Direct inspection: enumerate, grep, read, inspect, compare, follow.

These run on a *pinned revision*, never on "whatever is there now", and they
account for what they did not look at. A grep result says which files it
scanned and which it skipped and why, how many matches it returned and how
many it scanned, and whether the scan finished; only a finished scan may
support an absence or a count. A capped scan returns a cursor that resumes at
the exact match after the last one returned, at the same revision.
"""

from __future__ import annotations

import base64
import fnmatch
import io
import json
import re
import time
from typing import Any

from pheasant_harness.common import HarnessError, digest_bytes, digest_value
from pheasant_harness.connections import ConnectionDenied, QuotaExhausted
from pheasant_harness.registry import EntryUnreadable, Revision, RevisionMoved

MAX_READ_LINES = 400
MAX_READ_BYTES = 64 * 1024
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")


def _encode_cursor(value: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, sort_keys=True).encode()).decode()


def _decode_cursor(cursor: str | None) -> dict | None:
    if not cursor:
        return None
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except Exception as exc:  # noqa: BLE001 - a cursor is caller input
        raise HarnessError("cursor is not one this tool issued") from exc


def _matches_globs(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path.split("/")[-1], g) for g in globs)


def enumerate_entries(
    revision: Revision, prefix: str = "", cursor: str | None = None, limit: int = 200
) -> dict:
    position = int((_decode_cursor(cursor) or {}).get("offset", 0))
    chosen = [e for e in revision.entries if e["path"].startswith(prefix)]
    page = chosen[position : position + max(1, min(limit, 1000))]
    following = position + len(page)
    return {
        "outcome": "success" if following >= len(chosen) else "partial",
        "revision": revision.revision,
        "entries": [
            {
                "path": e["path"],
                "kind": e["kind"],
                "size": e.get("size"),
                "digest": e.get("digest"),
                "hidden": e.get("hidden", False),
                "ignored": e.get("ignored", False),
                "binary": e.get("binary", False),
                "generated": e.get("generated", False),
                "reason": e.get("reason"),
                "locator": {"registration_id": revision.registration_id, "path": e["path"]},
            }
            for e in page
        ],
        "total": len(chosen),
        "unreadable": [e["path"] for e in page if e["kind"] == "unreadable"],
        "next_cursor": _encode_cursor({"offset": following}) if following < len(chosen) else None,
    }


def _compile(pattern: str, options: dict) -> re.Pattern:
    flags = re.IGNORECASE if options.get("case_insensitive") else 0
    if options.get("regex"):
        try:
            return re.compile(pattern, flags)
        except re.error as exc:
            raise HarnessError(f"invalid regular expression: {exc}") from exc
    return re.compile(re.escape(pattern), flags)


def grep(
    resolver: Any,
    revision: Revision,
    pattern: str,
    options: dict | None = None,
    cursor: str | None = None,
) -> dict:
    options = dict(options or {})
    if not pattern:
        raise HarnessError("grep needs a non-empty pattern")
    include = list(options.get("include") or [])
    exclude = list(options.get("exclude") or [])
    page_size = max(1, min(int(options.get("max_matches", 50)), 500))
    context_lines = max(0, min(int(options.get("context", 1)), 5))
    max_file_bytes = int(options.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES))
    timeout = float(options.get("timeout_seconds", 20.0))
    generated_policy = options.get("generated", "include")
    compiled = _compile(pattern, options)
    query_digest = digest_value(
        {"pattern": pattern, "options": {k: v for k, v in options.items() if k != "max_matches"}}
    )

    state = _decode_cursor(cursor)
    if state and (state["revision"] != revision.revision or state["query"] != query_digest):
        raise HarnessError("cursor belongs to a different revision or query")
    start_entry = state["entry"] if state else 0
    # The exact position of the last match returned in the resumed entry:
    # (line, index of the match on that line). Everything at or before it was
    # returned already; everything after it was not.
    after = tuple(state["after"]) if state and state.get("after") else None
    carried = state["file_matches"] if state else 0
    coverage = (
        state["coverage"]
        if state
        else {
            "entries_total": len(revision.entries),
            "scanned_files": 0,
            "files_with_matches": 0,
            "matches_scanned": 0,
            "excluded_by_glob": 0,
            "hidden_skipped": 0,
            "ignored_skipped": 0,
            "generated_skipped": 0,
            "generated_scanned": 0,
            "binary_skipped": 0,
            "too_large": [],
            "unreadable": [],
            "changed": [],
            "listing_gaps": [],
        }
    )
    matches: list[dict] = []
    deadline = time.monotonic() + timeout
    next_cursor = None
    timed_out_from = None
    entries = revision.entries
    index = start_entry
    while index < len(entries):
        entry = entries[index]
        path = entry["path"]
        resumed = after is not None and index == start_entry
        if len(matches) >= page_size:
            # The page filled at a file boundary: the next page starts here.
            next_cursor = {"entry": index, "after": None, "file_matches": 0}
            break
        if time.monotonic() > deadline:
            timed_out_from = path
            break
        if not resumed:
            reason = _skip_reason(
                entry, include, exclude, options, generated_policy, max_file_bytes
            )
            if reason:
                _count_skip(coverage, reason, path)
                index += 1
                continue
        try:
            data = resolver.read_bytes(revision, path)
        except EntryUnreadable as exc:
            coverage["unreadable"].append({"path": path, "reason": str(exc)})
            index += 1
            continue
        except RevisionMoved:
            coverage["changed"].append(path)
            index += 1
            continue
        except QuotaExhausted:
            raise  # the run's budget, not this file: the scan stops and says why
        except ConnectionDenied as exc:
            # A file the connection's policy does not let us read is not scanned, and says so.
            coverage["unreadable"].append({"path": path, "reason": f"denied by policy: {exc}"})
            index += 1
            continue
        lines = data.decode("utf-8", errors="replace").splitlines()
        file_matches = carried if resumed else 0
        stop_at = None
        for line_number, line in enumerate(lines, start=1):
            for k, found in enumerate(compiled.finditer(line)):
                if resumed and (line_number, k) <= after:
                    continue
                if len(matches) >= page_size:
                    stop_at = matches[-1]["position"]
                    break
                file_matches += 1
                coverage["matches_scanned"] += 1
                lo = max(0, line_number - 1 - context_lines)
                matches.append(
                    {
                        "path": path,
                        "line": line_number,
                        "column": found.start() + 1,
                        "position": [line_number, k],
                        "text": line[:400],
                        "match": found.group(0)[:200],
                        "context": {
                            "before": lines[lo : line_number - 1],
                            "after": lines[line_number : line_number + context_lines],
                        },
                        "locator": {
                            "registration_id": revision.registration_id,
                            "revision": revision.revision,
                            "path": path,
                            "lines": [line_number, line_number],
                        },
                    }
                )
            if stop_at is not None:
                break
        if stop_at is not None:
            next_cursor = {"entry": index, "after": stop_at, "file_matches": file_matches}
            break
        coverage["scanned_files"] += 1
        if entry.get("generated"):
            coverage["generated_scanned"] += 1
        if file_matches:
            coverage["files_with_matches"] += 1
        index += 1
        after = None
    if timed_out_from is not None:
        next_cursor = {"entry": index, "after": None, "file_matches": 0}

    listing_gaps = coverage.setdefault("listing_gaps", [])
    complete = next_cursor is None and not listing_gaps
    coverage_out = {
        **coverage,
        "complete": complete,
        "more_pages": next_cursor is not None,
        "timed_out_from": timed_out_from,
        "matches_returned": len(matches),
        "options": {
            "include": include,
            "exclude": exclude,
            "hidden": bool(options.get("hidden")),
            "respect_ignore": not options.get("ignored"),
            "case_insensitive": bool(options.get("case_insensitive")),
            "regex": bool(options.get("regex")),
            "generated": generated_policy,
            "binary": "skipped",
            "encoding": "utf-8 (undecodable bytes replaced)",
            "max_file_bytes": max_file_bytes,
            "page_size": page_size,
        },
    }
    coverage_out["statement"] = coverage_statement(pattern, revision, coverage_out)
    payload = {
        "outcome": "success" if complete else "partial",
        "revision": revision.revision,
        "pattern": pattern,
        "matches": matches,
        "coverage": coverage_out,
        "next_cursor": None,
    }
    if next_cursor is not None:
        payload["next_cursor"] = _encode_cursor(
            {
                "revision": revision.revision,
                "query": query_digest,
                "entry": next_cursor["entry"],
                "after": next_cursor["after"],
                "file_matches": next_cursor["file_matches"],
                "coverage": coverage,
            }
        )
    return payload


def _skip_reason(entry, include, exclude, options, generated_policy, max_file_bytes) -> str | None:
    path = entry["path"]
    if entry.get("listing_gap"):
        return "listing_gap:" + str(entry.get("reason"))
    if entry["kind"] == "unreadable":
        return "unreadable:" + str(entry.get("reason"))
    if include and not _matches_globs(path, include):
        return "glob"
    if exclude and _matches_globs(path, exclude):
        return "glob"
    if entry.get("hidden") and not options.get("hidden"):
        return "hidden"
    if entry.get("ignored") and not options.get("ignored"):
        return "ignored"
    if entry.get("binary"):
        return "binary"
    if entry.get("generated") and generated_policy == "skip":
        return "generated"
    if (entry.get("size") or 0) > max_file_bytes:
        return "too_large"
    return None


def _count_skip(coverage: dict, reason: str, path: str) -> None:
    if reason.startswith("listing_gap:"):
        coverage.setdefault("listing_gaps", []).append(reason.split(":", 1)[1])
    elif reason.startswith("unreadable:"):
        coverage["unreadable"].append({"path": path, "reason": reason.split(":", 1)[1]})
    elif reason == "too_large":
        coverage["too_large"].append(path)
    else:
        key = {
            "glob": "excluded_by_glob",
            "hidden": "hidden_skipped",
            "ignored": "ignored_skipped",
            "binary": "binary_skipped",
            "generated": "generated_skipped",
        }[reason]
        coverage[key] += 1


def coverage_statement(pattern: str, revision: Revision, coverage: dict) -> str:
    rev = revision.revision.removeprefix("sha256:")[:12]
    skipped = []
    for key, label in (
        ("excluded_by_glob", "excluded by glob"),
        ("hidden_skipped", "hidden"),
        ("ignored_skipped", "ignored"),
        ("binary_skipped", "binary"),
        ("generated_skipped", "generated"),
    ):
        if coverage[key]:
            skipped.append(f"{coverage[key]} {label}")
    if coverage["unreadable"]:
        skipped.append(f"{len(coverage['unreadable'])} unreadable")
    if coverage["too_large"]:
        skipped.append(f"{len(coverage['too_large'])} too large")
    if coverage["changed"]:
        skipped.append(f"{len(coverage['changed'])} changed since resolve")
    tail = f" Not scanned: {', '.join(skipped)}." if skipped else ""
    scanned = coverage["scanned_files"]
    if (
        coverage.get("listing_gaps")
        and not coverage.get("timed_out_from")
        and not coverage.get("more_pages")
    ):
        return (
            f"Scan for {pattern!r} at revision {rev} covered only what the provider listed "
            f"({'; '.join(coverage['listing_gaps'])}): matches in "
            f"{coverage['files_with_matches']} of {scanned} scanned text files. No count or "
            f"absence over the whole source follows.{tail}"
        )
    if not coverage["complete"]:
        why = (
            f"timed out at {coverage['timed_out_from']}"
            if coverage.get("timed_out_from")
            else f"result cap reached after {coverage['matches_returned']} matches"
        )
        return (
            f"Partial scan for {pattern!r} at revision {rev}: {why}; {scanned} files scanned "
            f"so far. No count or absence follows from this page.{tail}"
        )
    if coverage["matches_scanned"] == 0:
        return (
            f"No occurrences of {pattern!r} in these {scanned} scanned text files at "
            f"revision {rev}.{tail}"
        )
    return (
        f"{coverage['matches_scanned']} "
        f"occurrence{'s' if coverage['matches_scanned'] != 1 else ''} "
        f"of {pattern!r} in "
        f"{coverage['files_with_matches']} of {scanned} scanned text files at revision {rev}.{tail}"
    )


def read_range(
    resolver: Any, revision: Revision, path: str, start: int | None = None, end: int | None = None
) -> dict:
    data = resolver.read_bytes(revision, path)
    entry = revision.entry(path) or {}
    if entry.get("binary"):
        return {
            "outcome": "partial",
            "path": path,
            "revision": revision.revision,
            "binary": True,
            "size": len(data),
            "digest": digest_bytes(data),
            "content": None,
            "note": "binary entry: use inspect for media",
        }
    lines = data.decode("utf-8", errors="replace").splitlines()
    first = max(1, int(start or 1))
    last = min(len(lines), int(end or first + MAX_READ_LINES - 1))
    truncated = False
    if last - first + 1 > MAX_READ_LINES:
        last, truncated = first + MAX_READ_LINES - 1, True
    chosen = lines[first - 1 : last]
    body = "\n".join(chosen)
    if len(body.encode()) > MAX_READ_BYTES:
        body, truncated = body.encode()[:MAX_READ_BYTES].decode("utf-8", errors="ignore"), True
    return {
        "outcome": "partial" if truncated else "success",
        "path": path,
        "revision": revision.revision,
        "lines": [first, last],
        "total_lines": len(lines),
        "content": body,
        "truncated": truncated,
        "digest": digest_bytes(data),
        "locator": {
            "registration_id": revision.registration_id,
            "revision": revision.revision,
            "path": path,
            "lines": [first, last],
        },
        "provenance": {"tool": "read", "resolver": resolver.name},
    }


def inspect_media(
    resolver: Any, revision: Revision, path: str, region: dict | None, blobs: Any
) -> dict:
    data = resolver.read_bytes(revision, path)
    result: dict[str, Any] = {
        "path": path,
        "revision": revision.revision,
        "asset_digest": digest_bytes(data),
        "derivatives": [],
    }
    for suffix in (".annotations.json", ".caption.txt", ".transcript.txt"):
        sidecar = revision.entry(path + suffix)
        if sidecar and sidecar["kind"] == "file":
            raw = resolver.read_bytes(revision, path + suffix)
            result["derivatives"].append(
                {
                    "path": path + suffix,
                    "digest": digest_bytes(raw),
                    "kind": suffix.strip(".").split(".")[0],
                    "content": json.loads(raw) if suffix.endswith(".json") else raw.decode(),
                    "mapping": "pixel coordinates of the original asset",
                }
            )
    if not path.lower().endswith(IMAGE_SUFFIXES):
        result["media_type"] = "application/octet-stream"
        return {"outcome": "partial", **result, "note": "no inspector for this media type"}
    try:
        from PIL import Image, ImageStat
    except ImportError:  # pragma: no cover - Pillow ships with the image
        return {"outcome": "partial", **result, "note": "image inspector unavailable"}
    image = Image.open(io.BytesIO(data))
    result.update(
        {
            "media_type": Image.MIME.get(image.format or "", "image"),
            "dimensions": {"width": image.width, "height": image.height},
            "coordinate_system": "pixels, origin top-left",
            "extractor": f"Pillow {getattr(__import__('PIL'), '__version__', '?')}",
        }
    )
    if region:
        box = (
            int(region["x"]),
            int(region["y"]),
            int(region["x"]) + int(region["w"]),
            int(region["y"]) + int(region["h"]),
        )
        if box[2] > image.width or box[3] > image.height or box[0] < 0 or box[1] < 0:
            raise HarnessError(f"region {box} lies outside {image.width}x{image.height}")
        crop = image.convert("L").crop(box)
        stats = ImageStat.Stat(crop)
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        crop_digest = blobs.put(buffer.getvalue(), "image/png")
        contrast = round(stats.stddev[0], 2)
        result["region"] = {
            "box": list(box),
            "crop_digest": crop_digest,
            "mean_luminance": round(stats.mean[0], 2),
            "contrast": contrast,
            "min": crop.getextrema()[0],
            "max": crop.getextrema()[1],
            "legibility": "faint" if contrast < 18 else "clear",
            "derived_from": result["asset_digest"],
        }
    return {"outcome": "success", **result}


def compare(resolver: Any, older: Revision, newer: Revision) -> dict:
    a = {e["path"]: e.get("digest") for e in older.entries}
    b = {e["path"]: e.get("digest") for e in newer.entries}
    return {
        "outcome": "success",
        "from": older.revision,
        "to": newer.revision,
        "added": sorted(b.keys() - a.keys()),
        "removed": sorted(a.keys() - b.keys()),
        "changed": sorted(p for p in a.keys() & b.keys() if a[p] != b[p]),
    }


def follow_symbol(resolver: Any, revision: Revision, symbol: str) -> dict:
    """Definitions and call sites of one identifier, by text pattern.

    A text pattern misses aliases, dynamic dispatch and generated bindings; the
    result says so, so nobody reads "no callers" as "no code path".
    """

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
        raise HarnessError("follow takes one identifier")
    definition = grep(
        resolver,
        revision,
        rf"^\s*(def|class|function|const|let|var|export\s+function)\s+{symbol}\b",
        {"regex": True, "max_matches": 50, "context": 2},
    )
    callers = grep(
        resolver, revision, rf"\b{symbol}\s*\(", {"regex": True, "max_matches": 100, "context": 1}
    )
    defined = {(m["path"], m["line"]) for m in definition["matches"]}
    calls = [m for m in callers["matches"] if (m["path"], m["line"]) not in defined]
    return {
        "outcome": "success" if callers["coverage"]["complete"] else "partial",
        "symbol": symbol,
        "definitions": definition["matches"],
        "callers": calls,
        "coverage": callers["coverage"],
        "limitations": (
            "Text-pattern resolution: aliases, dynamic dispatch, generated bindings and runtime "
            "configuration are not seen. A behavioral claim needs an executed check."
        ),
    }
