"""Landing submitted bytes into a real, indexable source.

The point of this module is that submitting a document is **not** a second
ingestion path. Files are written into a directory under ``/state/uploads``,
that directory is registered as an ordinary ``document_folder`` source, and
everything after that is the normal connector → parse → chunk → graph
pipeline: same idempotency (content sha256), same manifests, same incremental
skip, same graph edges. Deleting the source removes it like any other.

What this module owns is only the part that is genuinely different: taking a
filename supplied by an untrusted caller and turning it into a path we are
willing to write to.

**It lives in `ingestion`, not in `api`.** It began as `api/uploads.py`,
serving the browser drop-zone, and nothing in it was ever about HTTP —
`safe_filename` defends a filesystem, not a request. That was invisible while
the only caller was a route. It stopped being invisible when
`services/ingestion.py` needed the same placement rules for an agent's
submission: a service importing a transport is the upward edge
`tests/test_service_layering.py` exists to refuse, and the honest fix is that
the code was never in the right layer, not that the rule is inconvenient.
`api/uploads.py` re-exports these names, because the route and the UI's
vocabulary did not change.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

#: Windows reserves these device names in every directory, with or without an
#: extension. A file called ``con.txt`` is not creatable there, and a state
#: directory that is not portable is a state directory that breaks on restore.
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

#: Characters that make a filename dangerous or unportable, as a **denylist**.
#:
#: An allowlist of ``[A-Za-z0-9._ -]`` was the obvious first move and the wrong
#: one: it silently mangles every legitimate non-English filename —
#: ``Übersicht.pdf`` becomes ``_bersicht.pdf`` and ``会議メモ.md`` becomes
#: ``___.md`` — while defending against nothing that this list does not.
#: The actual hazards are path separators (escaping the directory), control
#: characters, and the characters Windows refuses in a filename.
_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')

#: Long enough for real document names, short enough to stay under the 255-byte
#: filename limit once a numeric de-duplication suffix is appended.
MAX_NAME_LENGTH = 180


def safe_filename(raw: str, *, fallback: str = "upload") -> str:
    """A filename we are willing to write, derived from an untrusted one.

    Everything here defends the same property: the result is a *single path
    component* that lands inside the target directory and nowhere else.

    * Only the basename survives, so ``../../etc/passwd`` and
      ``C:\\Windows\\system32\\x`` both reduce to their last component. This is
      belt-and-braces — the caller also joins under a resolved root — but a
      filename that has to be checked twice to be safe is a filename that
      should have been cleaned once.
    * Unicode is NFC-normalised, so two visually identical names cannot land on
      two different files (or, worse, collide on a case-folding filesystem
      after we thought we had de-duplicated them). Non-ASCII characters are
      *kept* — see :data:`_UNSAFE` for why an allowlist was the wrong tool.
    * A leading dot is dropped: an upload has no business creating ``.env`` or
      ``.git``, and the default exclude list would silently skip it anyway.

    The length cap counts **bytes**, not characters: filesystem limits are in
    bytes, and one emoji is four of them, so a character-counted cap lets a
    perfectly legal-looking name blow past the limit on ext4.
    """
    name = unicodedata.normalize("NFC", str(raw or ""))
    name = name.replace("\\", "/").split("/")[-1].strip()
    name = _UNSAFE.sub("_", name).strip(" .")
    if not name:
        return fallback
    stem, dot, suffix = name.partition(".")
    if stem.lower() in _WINDOWS_RESERVED:
        stem = f"{stem}_file"
    name = f"{stem}{dot}{suffix}"
    return _truncate(name, fallback)


def _truncate(name: str, fallback: str) -> str:
    """Cap the name at :data:`MAX_NAME_LENGTH` *bytes*, keeping the extension.

    Truncation is done on the encoded form and decoded back with errors
    ignored, so cutting mid-codepoint drops that character rather than
    producing an undecodable name.
    """
    if len(name.encode("utf-8")) <= MAX_NAME_LENGTH:
        return name or fallback
    head, _, tail = name.rpartition(".")
    suffix = f".{tail}" if head and len(tail.encode("utf-8")) < 16 else ""
    body = head if suffix else name
    budget = MAX_NAME_LENGTH - len(suffix.encode("utf-8"))
    clipped = body.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return (clipped + suffix) or fallback


def unique_path(directory: Path, filename: str) -> Path:
    """``directory/filename``, suffixed if needed so nothing is overwritten.

    Uploading ``notes.md`` twice gives ``notes.md`` and ``notes-2.md`` rather
    than one file and a lost document. Content-identical re-uploads still cost
    nothing downstream: the pipeline hashes content, so the second copy is
    indexed as its own artifact but re-syncs skip both.
    """
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(2, 10_000):
        alternative = directory / f"{stem}-{index}{suffix}"
        if not alternative.exists():
            return alternative
    raise ValueError(f"too many files named like {filename!r}")


def upload_root(state_path: Path, source_name: str) -> Path:
    """The directory backing an upload source. Created on demand."""
    root = Path(state_path) / "uploads" / safe_filename(source_name, fallback="uploads")
    root.mkdir(parents=True, exist_ok=True)
    return root


@dataclass
class StoredUpload:
    filename: str
    path: str
    size_bytes: int


def store_upload(
    directory: Path,
    filename: str,
    data: bytes,
    *,
    max_bytes: int | None = None,
) -> StoredUpload:
    """Write one uploaded file, refusing anything over ``max_bytes``.

    The size check happens before the write, not after: accepting the bytes and
    then deleting them still means the disk held them.
    """
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(
            f"{filename} is {len(data) // (1024 * 1024)} MB, over the "
            f"{max_bytes // (1024 * 1024)} MB per-file limit (sync.limits.max_file_size_mb)"
        )
    if not data:
        raise ValueError(f"{filename} is empty")
    directory.mkdir(parents=True, exist_ok=True)
    target = unique_path(directory, safe_filename(filename))
    target.write_bytes(data)
    return StoredUpload(filename=target.name, path=str(target), size_bytes=len(data))
