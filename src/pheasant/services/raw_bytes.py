"""A document's original bytes, served more than once without re-reading them.

``source_browser.raw_document`` serves a file only while it still holds the
bytes that were indexed, which it established by hashing the whole file on
every request. That was invisible while a viewer downloaded a PDF in one
request. pdf.js reading by range makes dozens of requests per document, and
each one re-hashed the whole file -- a 1 KB range of a 3 MB file took 236 ms
on the Azure api tier.

Two caches, both per process and both bounded:

- **A file's verified digest**, keyed by ``(path, size, mtime_ns)``. The
  stat is what changes when the file does, so an edited file is hashed
  again and a stale digest is never reused.
- **A ZIP member's inflated bytes**, keyed by the archive's
  ``(path, size, mtime_ns)``, the member and the artifact's sha256. A member
  has no file to seek in, so serving ``bytes=a-b`` of it means inflating it
  first; without this every range of a 58 MB book inflated all 58 MB. The
  archive's stat is in the key for the same reason as a file's: a rewritten
  archive is read again, so it is never served from what it used to hold.
  Least recently used goes first once ``MEMBER_CACHE_BYTES`` is exceeded,
  and a member larger than that is served without being kept.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

#: Inflated ZIP members held per process, in bytes.
MEMBER_CACHE_BYTES = 128 * 1024 * 1024
#: Verified file digests held per process.
DIGEST_CACHE_ENTRIES = 1024

_lock = threading.Lock()
_digests: OrderedDict[tuple[str, int, int], str] = OrderedDict()
_members: OrderedDict[tuple[str, int, int, str, str], bytes] = OrderedDict()
_member_bytes = 0


def file_sha256(path: Path) -> str | None:
    """The file's sha256, hashed once per (size, mtime) it has had."""

    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    with _lock:
        cached = _digests.get(key)
        if cached is not None:
            _digests.move_to_end(key)
            return cached
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except OSError:
        return None
    value = digest.hexdigest()
    with _lock:
        _digests[key] = value
        while len(_digests) > DIGEST_CACHE_ENTRIES:
            _digests.popitem(last=False)
    return value


def member_bytes(
    archive: Path, member: str, sha256: str, read: Callable[[], bytes | None]
) -> bytes | None:
    """A member's verified bytes, cached while the archive is unchanged.

    ``read`` must return the bytes only when they hash to ``sha256`` (as
    ``source_browser._indexed_bytes`` does), so the cache never holds bytes
    other than the ones its key names.
    """

    global _member_bytes
    try:
        stat = archive.stat()
    except OSError:
        return None
    key = (str(archive), stat.st_size, stat.st_mtime_ns, member, sha256)
    with _lock:
        cached = _members.get(key)
        if cached is not None:
            _members.move_to_end(key)
            return cached
    content = read()
    if content is None or len(content) > MEMBER_CACHE_BYTES:
        return content
    with _lock:
        if key not in _members:
            _members[key] = content
            _member_bytes += len(content)
        while _member_bytes > MEMBER_CACHE_BYTES:
            _, evicted = _members.popitem(last=False)
            _member_bytes -= len(evicted)
    return content


def clear() -> None:
    """For tests: forget both caches."""

    global _member_bytes
    with _lock:
        _digests.clear()
        _members.clear()
        _member_bytes = 0
