"""Read supported files from ZIP archives without extracting to disk."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

from pheasant.ingestion.content_types import (
    AUDIO_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    TEXT_EXTENSIONS,
)

SUPPORTED_MEMBER_EXTENSIONS = (
    TEXT_EXTENSIONS | DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS
)

# These remain in force when a caller disables the ordinary source budget.
# ZIP headers are untrusted, so a decompression bomb must still have a bound.
MAX_ARCHIVE_ENTRIES = 100_000
MAX_ARCHIVE_MEMBER_BYTES = 1024**3


class ArchiveError(ValueError):
    """An archive cannot be indexed safely or read consistently."""


def safe_member_name(info: zipfile.ZipInfo) -> str | None:
    """A relative, regular-file path that can become a stable artifact ID."""

    name = info.filename.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    if info.is_dir() or info.flag_bits & 1 or not name or any(ord(c) < 32 for c in name):
        return None
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        return None
    if info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16):
        return None
    if info.file_size < 0:
        return None
    if Path(name).suffix.lower() not in SUPPORTED_MEMBER_EXTENSIONS:
        return None
    return name


def members(path: Path) -> list[tuple[str, zipfile.ZipInfo]]:
    """List candidate files in stable order and refuse ambiguous names."""

    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ArchiveError(f"cannot read ZIP archive {path}: {exc}") from exc
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ArchiveError(f"ZIP archive {path} has more than {MAX_ARCHIVE_ENTRIES} entries")
    found: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        name = safe_member_name(info)
        if name is None:
            continue
        if name in found:
            raise ArchiveError(f"ZIP archive {path} contains duplicate member {name!r}")
        found[name] = info
    return sorted(found.items())


def read_member(path: Path, name: str, max_bytes: int) -> bytes:
    """Inflate one member within its declared and actual size bound."""

    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(name)
            if safe_member_name(info) is None or info.file_size > max_bytes:
                raise ArchiveError(f"ZIP member {name!r} is unsafe or exceeds the file limit")
            with archive.open(info) as stream:
                content = stream.read(max_bytes + 1)
    except (OSError, zipfile.BadZipFile, RuntimeError, KeyError) as exc:
        raise ArchiveError(f"cannot read ZIP member {name!r} from {path}: {exc}") from exc
    if len(content) > max_bytes or len(content) != info.file_size:
        raise ArchiveError(f"ZIP member {name!r} exceeds its declared size or the file limit")
    return content
