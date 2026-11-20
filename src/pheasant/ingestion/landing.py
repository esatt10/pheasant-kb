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


def safe_relative_path(raw: str, *, fallback: str = "upload") -> str:
    """A relative path that stays inside the upload directory, folders kept.

    Each component is cleaned by :func:`safe_filename` (no leading dot, no
    separators, no reserved names) and ``.``/``..``/empty components are
    dropped, so ``../../etc/x`` is ``etc/x`` and nothing can leave the
    directory. Folders are kept because a collection is more than its files:
    an Open Knowledge Format bundle uploaded as a folder is a tree whose
    ``index.md`` files and relative links only mean something in place, and
    flattening it to basenames turned three ``index.md`` into ``index-2.md``
    and ``index-3.md`` and broke every link.
    """

    raw_parts = unicodedata.normalize("NFC", str(raw or "")).replace("\\", "/").split("/")
    parts = [part for part in raw_parts if part.strip() not in ("", ".", "..")]
    if not parts:
        return safe_filename("", fallback=fallback)
    return "/".join(safe_filename(part, fallback=fallback) for part in parts)


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


def owned_upload_directory(
    state_path: Path,
    source_name: str,
    source_type: str,
    source_path: str | Path,
) -> Path | None:
    """Return the upload namespace owned by this source, if its path proves it.

    Ordinary folder sources point at user data and must never be deleted as a
    side effect of removing their index. UI uploads are different: each source
    has one reserved child of ``<state>/uploads``. Require the registered path
    to resolve to that exact child and refuse symlinks before returning it.
    """
    if source_type != "document_folder":
        return None

    state_root = Path(state_path).resolve()
    upload_base = state_root / "uploads"
    if upload_base.is_symlink():
        return None

    candidate = upload_base / safe_filename(source_name, fallback="uploads")
    if candidate.parent != upload_base or candidate.is_symlink():
        return None

    registered = Path(source_path)
    if registered.is_symlink():
        return None
    try:
        if registered.resolve() != candidate.resolve():
            return None
    except OSError:
        return None
    return candidate


@dataclass
class StoredUpload:
    filename: str
    path: str
    size_bytes: int
    #: The same name already held these exact bytes, so nothing was written.
    unchanged: bool = False


def check_submission_bytes(name: str, data: bytes, max_bytes: int | None) -> None:
    """Refuse bytes that may not be written, before anything writes them.

    One spelling, called from three places: this module's own writer, the
    forwarding client (which refuses before spending a cluster's bandwidth to
    reach the same answer), and the far side of that hop (which must not trust
    the client's check — a server that only validates what a well-behaved
    caller already validated is not validating). Two spellings of one refusal
    is the same defect as two implementations of one operation, one level down.

    The check happens before the write, not after: accepting the bytes and then
    deleting them still means the disk held them.
    """

    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(
            f"{name} is {len(data) // (1024 * 1024)} MB, over the "
            f"{max_bytes // (1024 * 1024)} MB per-file limit (sync.limits.max_file_size_mb)"
        )
    if not data:
        raise ValueError(f"{name} is empty")


def store_upload(
    directory: Path,
    filename: str,
    data: bytes,
    *,
    max_bytes: int | None = None,
) -> StoredUpload:
    """Write one uploaded file, refusing anything over ``max_bytes``.

    Idempotent by name and content: when ``filename`` (or a ``-2``, ``-3`` copy
    of it) already holds exactly these bytes, that file is the answer and
    nothing is written, so dropping the same file twice -- or a retry of a drop
    whose reply was lost -- stores it once. Different bytes under a name still
    take the next free suffix, as before.
    """
    check_submission_bytes(filename, data, max_bytes)
    # A file uploaded from a folder keeps its folder (`safe_relative_path`).
    relative = safe_relative_path(filename)
    *folders, name = relative.split("/")
    folder = directory.joinpath(*folders)
    folder.mkdir(parents=True, exist_ok=True)
    existing = identical_upload(folder, name, data)
    if existing is not None:
        return StoredUpload(
            filename=existing.relative_to(directory).as_posix(),
            path=str(existing),
            size_bytes=len(data),
            unchanged=True,
        )
    target = unique_path(folder, name)
    target.write_bytes(data)
    return StoredUpload(
        filename=target.relative_to(directory).as_posix(), path=str(target), size_bytes=len(data)
    )


def identical_upload(directory: Path, filename: str, data: bytes) -> Path | None:
    """The file named ``filename`` (or a numbered copy) already holding ``data``."""

    candidate = directory / filename
    names = [candidate] + [
        directory / f"{candidate.stem}-{index}{candidate.suffix}" for index in range(2, 10_000)
    ]
    for path in names:
        if not path.exists():
            return None
        try:
            if path.is_file() and path.stat().st_size == len(data) and path.read_bytes() == data:
                return path
        except OSError:
            continue
    return None
