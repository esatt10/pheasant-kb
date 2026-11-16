"""Read supported files from ZIP archives without extracting to disk.

An archive is a folder of its members. A ZIP inside a ZIP is a folder too: its
members are listed under it (``outer.zip/inner.zip/a.pdf``), to
:data:`MAX_ARCHIVE_DEPTH` archives deep. A nested archive is read into memory
to be opened, so it is bounded by :data:`MAX_NESTED_ARCHIVE_BYTES` as well as
by the source's file limit, and every entry of every level counts toward
:data:`MAX_ARCHIVE_ENTRIES`. A nested archive that cannot be opened (damaged,
encrypted, too large, too deep) is still listed, with the reason, so a sync
reports it as unreadable instead of leaving it out without a word -- which is
what an archive inside an upload used to do.

A member's *entry* spelling (``archive_entry_name``) names it inside each
archive on the way down, outermost first, joined by :data:`NESTED_SEPARATOR`.
No safe entry contains it: a name with an empty path component is refused.
"""

from __future__ import annotations

import io
import stat
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from pheasant.ingestion.content_types import (
    AUDIO_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    TEXT_EXTENSIONS,
    is_text_file,
)

SUPPORTED_MEMBER_EXTENSIONS = (
    TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS
)
ARCHIVE_EXTENSIONS = frozenset({".zip"})

# These remain in force when a caller disables the ordinary source budget.
# ZIP headers are untrusted, so a decompression bomb must still have a bound.
MAX_ARCHIVE_ENTRIES = 100_000
MAX_ARCHIVE_MEMBER_BYTES = 1024**3
#: Archives within archives, the outermost counted: a.zip/b.zip/c.zip/d.zip.
MAX_ARCHIVE_DEPTH = 4
#: A nested archive is inflated into memory to be opened.
MAX_NESTED_ARCHIVE_BYTES = 256 * 1024**2
NESTED_SEPARATOR = "//"


class ArchiveError(ValueError):
    """An archive cannot be indexed safely or read consistently."""


@dataclass(frozen=True)
class MemberInfo:
    """One listed member: where it is, how big it says it is, and, for a
    nested archive that could not be opened, why."""

    filename: str
    file_size: int
    error: str | None = None


@dataclass
class Listing:
    """What one archive holds: the members to index, and what was left out."""

    members: list[tuple[str, MemberInfo]] = field(default_factory=list)
    #: Entries of a type that is not indexed, by extension (``"(none)"``).
    skipped: Counter = field(default_factory=Counter)
    #: Nested archives opened and listed.
    nested: int = 0

    @property
    def unreadable(self) -> list[tuple[str, MemberInfo]]:
        return [(name, info) for name, info in self.members if info.error]


def _safe_path(info: zipfile.ZipInfo) -> str | None:
    """A relative, regular-file path that can become a stable artifact ID."""

    name = info.filename.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if info.is_dir() or not name or any(ord(c) < 32 for c in name):
        return None
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        return None
    if info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16):
        return None
    if info.file_size < 0:
        return None
    return name


def _supported(name: str) -> bool:
    return Path(name).suffix.lower() in SUPPORTED_MEMBER_EXTENSIONS or is_text_file(name)


def is_archive(name: str) -> bool:
    return Path(name).suffix.lower() in ARCHIVE_EXTENSIONS


def safe_member_name(info: zipfile.ZipInfo) -> str | None:
    """The member's safe path when it is a file this region indexes, else ``None``."""

    name = _safe_path(info)
    if name is None or info.flag_bits & 1 or not _supported(name):
        return None
    return name


def _open_nested(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> zipfile.ZipFile:
    if info.flag_bits & 1:
        raise ArchiveError("the archive is encrypted")
    if info.file_size > MAX_NESTED_ARCHIVE_BYTES:
        raise ArchiveError(
            f"the archive inflates to {info.file_size} bytes, more than the "
            f"{MAX_NESTED_ARCHIVE_BYTES} a nested archive may"
        )
    try:
        with archive.open(info) as stream:
            data = stream.read(MAX_NESTED_ARCHIVE_BYTES + 1)
        if len(data) != info.file_size:
            raise ArchiveError("the archive's size does not match its header")
        return zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError) as exc:
        raise ArchiveError(f"cannot open the nested archive: {exc}") from exc


def scan(path: Path) -> Listing:
    """List an archive's indexable members, nested archives' included, in stable order.

    Raises :class:`ArchiveError` when the outermost archive cannot be read,
    holds more than :data:`MAX_ARCHIVE_ENTRIES` entries in all, or names one
    member twice.
    """

    listing = Listing()
    found: dict[str, MemberInfo] = {}
    entries = 0

    def walk(archive: zipfile.ZipFile, prefix: str, chain: tuple[str, ...], depth: int) -> None:
        nonlocal entries
        infos = archive.infolist()
        entries += len(infos)
        if entries > MAX_ARCHIVE_ENTRIES:
            raise ArchiveError(f"ZIP archive {path} has more than {MAX_ARCHIVE_ENTRIES} entries")
        for info in infos:
            name = _safe_path(info)
            if name is None:
                continue
            virtual = prefix + name
            entry = NESTED_SEPARATOR.join((*chain, info.filename))
            if is_archive(name):
                error = None
                if depth + 1 >= MAX_ARCHIVE_DEPTH:
                    error = f"archives are expanded {MAX_ARCHIVE_DEPTH} deep at most"
                else:
                    try:
                        with _open_nested(archive, info) as inner:
                            listing.nested += 1
                            walk(inner, virtual + "/", (*chain, info.filename), depth + 1)
                        continue
                    except ArchiveError as exc:
                        error = str(exc)
                if virtual in found:
                    raise ArchiveError(f"ZIP archive {path} contains duplicate member {virtual!r}")
                found[virtual] = MemberInfo(entry, info.file_size, error)
                continue
            if info.flag_bits & 1:
                listing.skipped["encrypted"] += 1
                continue
            if not _supported(name):
                listing.skipped[Path(name).suffix.lower() or "(none)"] += 1
                continue
            if virtual in found:
                raise ArchiveError(f"ZIP archive {path} contains duplicate member {virtual!r}")
            found[virtual] = MemberInfo(entry, info.file_size)

    try:
        with zipfile.ZipFile(path) as archive:
            walk(archive, "", (), 0)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ArchiveError(f"cannot read ZIP archive {path}: {exc}") from exc
    listing.members = sorted(found.items())
    return listing


def members(path: Path) -> list[tuple[str, MemberInfo]]:
    """The members :func:`scan` lists, nested archives that failed included."""

    return scan(path).members


def find_member(path: Path, name: str) -> MemberInfo | None:
    """The member ``scan`` lists as ``name``, or ``None``.

    ``name`` is the safe spelling an artifact's path carries, which can differ
    from the entry's own (``./a.pdf``, ``dir\\a.pdf``), so the lookup applies
    the rule that produced it rather than ``getinfo``.
    """

    try:
        found = dict(members(path)).get(name)
    except ArchiveError:
        return None
    return None if found is None or found.error else found


def _read(archive: zipfile.ZipFile, entry: str, max_bytes: int, *, final: bool) -> bytes | IO:
    info = archive.getinfo(entry)
    if final:
        if safe_member_name(info) is None or info.file_size > max_bytes:
            raise ArchiveError(f"ZIP member {entry!r} is unsafe or exceeds the file limit")
        with archive.open(info) as stream:
            content = stream.read(max_bytes + 1)
        if len(content) > max_bytes or len(content) != info.file_size:
            raise ArchiveError(f"ZIP member {entry!r} exceeds its declared size or the file limit")
        return content
    if _safe_path(info) is None or not is_archive(_safe_path(info) or ""):
        raise ArchiveError(f"ZIP member {entry!r} is not a nested archive")
    return _open_nested(archive, info)


def read_member(path: Path, name: str, max_bytes: int) -> bytes:
    """Inflate one member within its declared and actual size bound.

    ``name`` is the entry spelling, nested archives' joined by
    :data:`NESTED_SEPARATOR`.
    """

    chain = name.split(NESTED_SEPARATOR)
    if len(chain) > MAX_ARCHIVE_DEPTH:
        raise ArchiveError(f"ZIP member {name!r} is nested too deep")
    try:
        with zipfile.ZipFile(path) as archive:
            opened: list[zipfile.ZipFile] = []
            current = archive
            try:
                for entry in chain[:-1]:
                    current = _read(current, entry, max_bytes, final=False)  # type: ignore[assignment]
                    opened.append(current)
                return _read(current, chain[-1], max_bytes, final=True)  # type: ignore[return-value]
            finally:
                for inner in reversed(opened):
                    inner.close()
    except (OSError, zipfile.BadZipFile, RuntimeError, KeyError) as exc:
        raise ArchiveError(f"cannot read ZIP member {name!r} from {path}: {exc}") from exc
