"""Read supported files from ZIP and tar archives without extracting to disk.

An archive is a folder of its members: a ZIP (``.zip``) or a tar, plain or
compressed (``.tar``, ``.tar.gz``/``.tgz``, ``.tar.bz2``/``.tbz2``/``.tbz``,
``.tar.xz``/``.txz``). An archive inside an archive is a folder too, whatever
the two formats: its members are listed under it (``outer.zip/inner.tgz/a.pdf``),
to :data:`MAX_ARCHIVE_DEPTH` archives deep. A nested archive is read into
memory to be opened, so it is bounded by :data:`MAX_NESTED_ARCHIVE_BYTES` as
well as by the source's file limit, and every entry of every level counts
toward :data:`MAX_ARCHIVE_ENTRIES`. A nested archive that cannot be opened
(damaged, encrypted, too large, too deep) is still listed, with the reason, so
a sync reports it as unreadable instead of leaving it out without a word.

**One set of rules, two formats.** Each format is a small adapter
(:class:`_Zip`, :class:`_Tar`) that lists entries and reads one; the safety
rules (no absolute or ``..`` paths, no links or devices, no encrypted
members), the nesting and the limits are written once, against the adapter.

**A tar is read by seeking, never re-decompressed per member.** A tar has no
central directory, so finding one member means walking the headers, and a
compressed tar can only be walked by decompressing from the start: reading
each of a ``.tar.gz``'s members by opening it afresh would decompress the
whole archive once per member. The outermost tar is therefore opened once per
process and kept (:func:`_outer_tar`), with its member index; a compressed one
is first decompressed, once, into a temporary file bounded by
:data:`MAX_TAR_INFLATED_BYTES` (a decompression bomb gets no further).

A member's *entry* spelling (``archive_entry_name``) names it inside each
archive on the way down, outermost first, joined by :data:`NESTED_SEPARATOR`.
No safe entry contains it: a name with an empty path component is refused.
"""

from __future__ import annotations

import io
import stat
import tarfile
import tempfile
import threading
import zipfile
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

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
#: Archive name endings, by format. Longest first where one ends another.
ZIP_SUFFIXES = (".zip",)
TAR_SUFFIXES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tgz",
    ".tbz2",
    ".tbz",
    ".txz",
    ".tar",
)
ARCHIVE_SUFFIXES = ZIP_SUFFIXES + TAR_SUFFIXES
#: Kept for callers that compare a single suffix; a compound one
#: (``.tar.gz``) needs :func:`is_archive`.
ARCHIVE_EXTENSIONS = frozenset({".zip", ".tar", ".tgz", ".tbz2", ".tbz", ".txz"})
#: Globs that find every archive in a folder walk, in both cases (the walk's
#: matching is case-sensitive).
ARCHIVE_GLOBS = tuple(
    pattern for suffix in ARCHIVE_SUFFIXES for pattern in (f"**/*{suffix}", f"**/*{suffix.upper()}")
)

# These remain in force when a caller disables the ordinary source budget.
# Archive headers are untrusted, so a decompression bomb must still have a bound.
MAX_ARCHIVE_ENTRIES = 100_000
MAX_ARCHIVE_MEMBER_BYTES = 1024**3
#: Archives within archives, the outermost counted: a.zip/b.zip/c.zip/d.zip.
MAX_ARCHIVE_DEPTH = 4
#: A nested archive is inflated into memory to be opened.
MAX_NESTED_ARCHIVE_BYTES = 256 * 1024**2
#: A compressed outermost tar is decompressed to a temporary file at most this big.
MAX_TAR_INFLATED_BYTES = 8 * 1024**3
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


# ------------------------------------------------------------------ formats


def archive_kind(name: str) -> str | None:
    """``"zip"``, ``"tar"`` or ``None``, by the name's ending."""

    lowered = str(name).lower()
    if lowered.endswith(ZIP_SUFFIXES):
        return "zip"
    if lowered.endswith(TAR_SUFFIXES):
        return "tar"
    return None


def is_archive(name: str) -> bool:
    return archive_kind(name) is not None


def archive_mime(name: str) -> str:
    lowered = str(name).lower()
    if lowered.endswith(ZIP_SUFFIXES):
        return "application/zip"
    if lowered.endswith((".tar.gz", ".tgz")):
        return "application/gzip"
    if lowered.endswith((".tar.bz2", ".tbz2", ".tbz")):
        return "application/x-bzip2"
    if lowered.endswith((".tar.xz", ".txz")):
        return "application/x-xz"
    return "application/x-tar"


@dataclass(frozen=True)
class _Entry:
    """One archive entry, as both formats describe it."""

    name: str
    size: int
    regular: bool
    link: bool
    encrypted: bool
    raw: Any


class _Zip:
    def __init__(self, source: Any) -> None:
        try:
            self.archive = zipfile.ZipFile(source)
        except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise ArchiveError(f"cannot read ZIP archive: {exc}") from exc

    def entries(self) -> list[_Entry]:
        return [self._entry(info) for info in self.archive.infolist()]

    @staticmethod
    def _entry(info: zipfile.ZipInfo) -> _Entry:
        link = info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16)
        return _Entry(
            name=info.filename,
            size=info.file_size,
            regular=not info.is_dir() and not link,
            link=link,
            encrypted=bool(info.flag_bits & 1),
            raw=info,
        )

    def get(self, name: str) -> _Entry:
        try:
            return self._entry(self.archive.getinfo(name))
        except KeyError as exc:
            raise ArchiveError(f"no member {name!r}") from exc

    def read(self, entry: _Entry, limit: int) -> bytes:
        try:
            with self.archive.open(entry.raw) as stream:
                return stream.read(limit + 1)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise ArchiveError(f"cannot read {entry.name!r}: {exc}") from exc

    def close(self) -> None:
        self.archive.close()


class _Tar:
    def __init__(self, source: Any, *, keep: Any = None) -> None:
        try:
            if isinstance(source, (str, Path)):
                self.archive = tarfile.open(source, mode="r:*")  # noqa: SIM115
            else:
                self.archive = tarfile.open(fileobj=source, mode="r:*")  # noqa: SIM115
            members = self.archive.getmembers()
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise ArchiveError(f"cannot read tar archive: {exc}") from exc
        self._keep = keep  # a temporary file this archive reads from
        self._lock = threading.Lock()
        self._entries = [self._entry(info) for info in members]
        self._by_name = {entry.name: entry for entry in self._entries}

    @staticmethod
    def _entry(info: tarfile.TarInfo) -> _Entry:
        return _Entry(
            name=info.name,
            size=info.size,
            regular=info.isreg(),
            link=info.issym() or info.islnk(),
            encrypted=False,
            raw=info,
        )

    def entries(self) -> list[_Entry]:
        return list(self._entries)

    def get(self, name: str) -> _Entry:
        found = self._by_name.get(name)
        if found is None:
            raise ArchiveError(f"no member {name!r}")
        return found

    def read(self, entry: _Entry, limit: int) -> bytes:
        # One file object, seeked per read: not safe from two threads at once.
        with self._lock:
            try:
                stream = self.archive.extractfile(entry.raw)
                if stream is None:
                    raise ArchiveError(f"{entry.name!r} is not a regular file")
                with stream:
                    return stream.read(limit + 1)
            except (OSError, tarfile.TarError, EOFError) as exc:
                raise ArchiveError(f"cannot read {entry.name!r}: {exc}") from exc

    def close(self) -> None:
        self.archive.close()
        if self._keep is not None:
            self._keep.close()


def _open_bytes(name: str, data: bytes) -> _Zip | _Tar:
    """A nested archive, from its bytes, by the format its name says."""

    kind = archive_kind(name)
    if kind == "zip":
        return _Zip(io.BytesIO(data))
    if kind == "tar":
        return _Tar(io.BytesIO(data))
    raise ArchiveError(f"{name!r} is not an archive this region reads")


# Outermost tars, opened once per process: (path, mtime, size) -> archive.
_TARS: OrderedDict[tuple[str, int, int], _Tar] = OrderedDict()
_TARS_LOCK = threading.Lock()
_TARS_KEPT = 2


def _outer_tar(path: Path) -> _Tar:
    """The outermost tar at ``path``, indexed once and read by seeking.

    A compressed one is decompressed once into a temporary file first: its
    stream cannot seek backwards, so every member read would otherwise
    decompress it again from the start.
    """

    try:
        info = path.stat()
    except OSError as exc:
        raise ArchiveError(f"cannot read tar archive {path}: {exc}") from exc
    key = (str(path), info.st_mtime_ns, info.st_size)
    with _TARS_LOCK:
        cached = _TARS.get(key)
        if cached is not None:
            _TARS.move_to_end(key)
            return cached
    compressed = not str(path).lower().endswith(".tar")
    if compressed:
        spool = tempfile.TemporaryFile()  # noqa: SIM115 - owned by the archive
        try:
            with tarfile.open(path, mode="r:*") as probe:
                raw = probe.fileobj
                raw.seek(0)
                copied = 0
                while chunk := raw.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > MAX_TAR_INFLATED_BYTES:
                        raise ArchiveError(
                            f"tar archive {path} inflates past {MAX_TAR_INFLATED_BYTES} bytes"
                        )
                    spool.write(chunk)
            spool.seek(0)
            opened = _Tar(spool, keep=spool)
        except (OSError, tarfile.TarError, EOFError) as exc:
            spool.close()
            raise ArchiveError(f"cannot read tar archive {path}: {exc}") from exc
        except ArchiveError:
            spool.close()
            raise
    else:
        opened = _Tar(path)
    with _TARS_LOCK:
        _TARS[key] = opened
        while len(_TARS) > _TARS_KEPT:
            # Dropped, not closed: another thread may still be reading it, and
            # it (and its temporary file) closes once nothing does.
            _TARS.popitem(last=False)
    return opened


def _open_outer(path: Path) -> tuple[_Zip | _Tar, bool]:
    """The outermost archive, and whether the caller should close it."""

    kind = archive_kind(path.name)
    if kind == "tar":
        return _outer_tar(path), False
    if kind == "zip":
        return _Zip(path), True
    raise ArchiveError(f"{path.name!r} is not an archive this region reads")


def close_cached() -> None:
    """Close the kept tars (tests, shutdown)."""

    with _TARS_LOCK:
        while _TARS:
            _, archive = _TARS.popitem()
            archive.close()


# -------------------------------------------------------------------- rules


def _safe_entry_path(entry: _Entry) -> str | None:
    """A relative, regular-file path that can become a stable artifact ID."""

    name = entry.name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if not entry.regular or entry.link or not name or any(ord(c) < 32 for c in name):
        return None
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        return None
    if entry.size < 0:
        return None
    return name


def _safe_path(info: zipfile.ZipInfo) -> str | None:
    """:func:`_safe_entry_path` for a ZIP entry (kept for callers of the ZIP spelling)."""

    return _safe_entry_path(_Zip._entry(info))


def _supported(name: str) -> bool:
    return Path(name).suffix.lower() in SUPPORTED_MEMBER_EXTENSIONS or is_text_file(name)


def safe_member_name(info: zipfile.ZipInfo) -> str | None:
    """The member's safe path when it is a file this region indexes, else ``None``."""

    entry = _Zip._entry(info)
    name = _safe_entry_path(entry)
    if name is None or entry.encrypted or not _supported(name):
        return None
    return name


def _open_nested(archive: _Zip | _Tar, entry: _Entry, name: str) -> _Zip | _Tar:
    if entry.encrypted:
        raise ArchiveError("the archive is encrypted")
    if entry.size > MAX_NESTED_ARCHIVE_BYTES:
        raise ArchiveError(
            f"the archive inflates to {entry.size} bytes, more than the "
            f"{MAX_NESTED_ARCHIVE_BYTES} a nested archive may"
        )
    data = archive.read(entry, MAX_NESTED_ARCHIVE_BYTES)
    if len(data) != entry.size:
        raise ArchiveError("the archive's size does not match its header")
    try:
        return _open_bytes(name, data)
    except ArchiveError as exc:
        raise ArchiveError(f"cannot open the nested archive: {exc}") from exc


# -------------------------------------------------------------------- reads


def scan(path: Path) -> Listing:
    """List an archive's indexable members, nested archives' included, in stable order.

    Raises :class:`ArchiveError` when the outermost archive cannot be read,
    holds more than :data:`MAX_ARCHIVE_ENTRIES` entries in all, or names one
    member twice.
    """

    listing = Listing()
    found: dict[str, MemberInfo] = {}
    entries = 0

    def walk(archive: _Zip | _Tar, prefix: str, chain: tuple[str, ...], depth: int) -> None:
        nonlocal entries
        listed = archive.entries()
        entries += len(listed)
        if entries > MAX_ARCHIVE_ENTRIES:
            raise ArchiveError(f"archive {path} has more than {MAX_ARCHIVE_ENTRIES} entries")
        for entry in listed:
            name = _safe_entry_path(entry)
            if name is None:
                continue
            virtual = prefix + name
            spelled = NESTED_SEPARATOR.join((*chain, entry.name))
            if is_archive(name):
                error = None
                if depth + 1 >= MAX_ARCHIVE_DEPTH:
                    error = f"archives are expanded {MAX_ARCHIVE_DEPTH} deep at most"
                else:
                    try:
                        inner = _open_nested(archive, entry, name)
                        try:
                            listing.nested += 1
                            walk(inner, virtual + "/", (*chain, entry.name), depth + 1)
                        finally:
                            inner.close()
                        continue
                    except ArchiveError as exc:
                        error = str(exc)
                if virtual in found:
                    raise ArchiveError(f"archive {path} contains duplicate member {virtual!r}")
                found[virtual] = MemberInfo(spelled, entry.size, error)
                continue
            if entry.encrypted:
                listing.skipped["encrypted"] += 1
                continue
            if not _supported(name):
                listing.skipped[Path(name).suffix.lower() or "(none)"] += 1
                continue
            if virtual in found:
                raise ArchiveError(f"archive {path} contains duplicate member {virtual!r}")
            found[virtual] = MemberInfo(spelled, entry.size)

    try:
        outer, owned = _open_outer(path)
    except ArchiveError as exc:
        raise ArchiveError(f"cannot read archive {path}: {exc}") from exc
    try:
        walk(outer, "", (), 0)
    finally:
        if owned:
            outer.close()
    listing.members = sorted(found.items())
    return listing


def members(path: Path) -> list[tuple[str, MemberInfo]]:
    """The members :func:`scan` lists, nested archives that failed included."""

    return scan(path).members


def find_member(path: Path, name: str) -> MemberInfo | None:
    """The member ``scan`` lists as ``name``, or ``None``.

    ``name`` is the safe spelling an artifact's path carries, which can differ
    from the entry's own (``./a.pdf``, ``dir\\a.pdf``), so the lookup applies
    the rule that produced it rather than a lookup by entry name.
    """

    try:
        found = dict(members(path)).get(name)
    except ArchiveError:
        return None
    return None if found is None or found.error else found


def _read(archive: _Zip | _Tar, name: str, max_bytes: int, *, final: bool) -> bytes | IO | Any:
    entry = archive.get(name)
    safe = _safe_entry_path(entry)
    if final:
        if safe is None or entry.encrypted or not _supported(safe) or entry.size > max_bytes:
            raise ArchiveError(f"member {name!r} is unsafe or exceeds the file limit")
        content = archive.read(entry, max_bytes)
        if len(content) > max_bytes or len(content) != entry.size:
            raise ArchiveError(f"member {name!r} exceeds its declared size or the file limit")
        return content
    if safe is None or not is_archive(safe):
        raise ArchiveError(f"member {name!r} is not a nested archive")
    return _open_nested(archive, entry, safe)


def read_member(path: Path, name: str, max_bytes: int) -> bytes:
    """Inflate one member within its declared and actual size bound.

    ``name`` is the entry spelling, nested archives' joined by
    :data:`NESTED_SEPARATOR`.
    """

    chain = name.split(NESTED_SEPARATOR)
    if len(chain) > MAX_ARCHIVE_DEPTH:
        raise ArchiveError(f"member {name!r} is nested too deep")
    try:
        outer, owned = _open_outer(path)
    except ArchiveError as exc:
        raise ArchiveError(f"cannot read member {name!r} from {path}: {exc}") from exc
    opened: list[Any] = []
    try:
        current: Any = outer
        for entry in chain[:-1]:
            current = _read(current, entry, max_bytes, final=False)
            opened.append(current)
        return _read(current, chain[-1], max_bytes, final=True)
    except ArchiveError as exc:
        raise ArchiveError(f"cannot read member {name!r} from {path}: {exc}") from exc
    finally:
        for inner in reversed(opened):
            inner.close()
        if owned:
            outer.close()


__all__ = [
    "ARCHIVE_EXTENSIONS",
    "ARCHIVE_GLOBS",
    "ARCHIVE_SUFFIXES",
    "ArchiveError",
    "Listing",
    "MAX_ARCHIVE_DEPTH",
    "MAX_ARCHIVE_ENTRIES",
    "MAX_ARCHIVE_MEMBER_BYTES",
    "MAX_NESTED_ARCHIVE_BYTES",
    "MemberInfo",
    "NESTED_SEPARATOR",
    "archive_kind",
    "archive_mime",
    "close_cached",
    "find_member",
    "is_archive",
    "members",
    "read_member",
    "safe_member_name",
    "scan",
]
