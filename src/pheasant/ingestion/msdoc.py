"""Text extraction for legacy Word 97-2003 binary ``.doc`` files.

The only format in this set that is not a ZIP or a text file. A ``.doc`` is an
OLE2 **Compound File Binary** container (a small FAT filesystem in a file)
holding a ``WordDocument`` stream plus a ``0Table``/``1Table`` stream, and the
document's text is *not* stored contiguously: the ``WordDocument`` stream holds
raw character data, and a **piece table** in the table stream says which byte
ranges make up the document in order and whether each piece is single-byte
cp1252 or UTF-16. Reading a ``.doc`` therefore means implementing both layers.

Why implement it rather than depend on something
------------------------------------------------
There is no standard-library support, and no third-party package already in
this project's dependency tree reads ``.doc``: ``python-docx`` handles only
the OOXML ``.docx``, and PDFium reads only PDF. The
alternative to this module is either a new dependency or a heuristic
"scrape printable runs out of the binary" hack. The latter is what makes
legacy extraction look broken — it interleaves field codes, style names and
binary noise with real prose. Following the piece table is what produces the
document's actual text in its actual order, so that is what this does.

Verified against a real producer
--------------------------------
Written and checked against ``.doc`` files written by a real office suite, not
from the specification alone. That distinction matters more here than for the
XML formats: a hand-authored CFB fixture would only prove that this module's
reader agrees with its own author's understanding of the format, which is no
evidence at all. The accompanying tests additionally cross-check the container
layer against an independent CFB implementation where one is installed.

Bounds
------
Every offset in the file is attacker-controlled, and this module does index
arithmetic with them. Python's bounds-checked slicing makes the failure mode a
raised exception rather than memory corruption, so the risk to contain is
resource exhaustion and non-termination: sector-chain walks carry a visited
set (a corrupt FAT can otherwise loop forever), and stream sizes, piece counts
and total text are all capped. A malformed file yields ``""``, never a hang.
"""

from __future__ import annotations

import logging
import struct

logger = logging.getLogger(__name__)

CFB_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_DIFSECT = 0xFFFFFFFC
_FATSECT = 0xFFFFFFFD

MAX_STREAM_BYTES = 64 * 1024 * 1024
MAX_PIECES = 200_000
MAX_TEXT_CHARS = 8 * 1024 * 1024

# Word's in-text control characters. Anything not listed is ordinary text.
_PARAGRAPH_END = 0x0D
_CELL_OR_ROW_END = 0x07
_LINE_BREAK = 0x0B
_PAGE_BREAK = 0x0C
_FIELD_BEGIN = 0x13
_FIELD_SEPARATOR = 0x14
_FIELD_END = 0x15
_NBSP = 0xA0
_NB_HYPHEN = 0x1E
_OPTIONAL_HYPHEN = 0x1F
# Reference/placeholder markers that stand in for non-text content: a picture
# anchor (0x01), an auto-numbered footnote/endnote reference (0x02), an
# annotation reference (0x05), a drawn object (0x08), and the unused low codes
# around them.
#
# Every entry MUST be a control code below 0x20. An earlier draft of this set
# also listed 0x28/0x3C/0x3E on the mistaken belief that they were Word
# markers; those are the printable ASCII "(", "<" and ">", so the effect was
# to silently delete parentheses and angle brackets from every document — text
# loss with no error, invisible unless a fixture happens to contain them.
_DROP = frozenset({0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x08})
assert all(code < 0x20 for code in _DROP), "_DROP must contain only control codes"


class CompoundFileError(ValueError):
    """The bytes are not a usable Compound File Binary container."""


class CompoundFile:
    """Minimal read-only Compound File Binary (OLE2) reader.

    Implements exactly what reading a ``.doc`` needs: the header, the FAT (via
    the DIFAT), the directory, and stream reads from either the normal sectors
    or the mini stream. No writing, no property sets, no storage traversal
    beyond finding named streams.
    """

    def __init__(self, data: bytes) -> None:
        if len(data) < 512 or not data.startswith(CFB_SIGNATURE):
            raise CompoundFileError("not a compound file (bad signature)")
        self._data = data

        sector_shift = struct.unpack_from("<H", data, 30)[0]
        mini_shift = struct.unpack_from("<H", data, 32)[0]
        if not 7 <= sector_shift <= 16 or not 2 <= mini_shift <= 12:
            raise CompoundFileError(f"implausible sector shifts {sector_shift}/{mini_shift}")
        self.sector_size = 1 << sector_shift
        self.mini_sector_size = 1 << mini_shift
        self.mini_cutoff = struct.unpack_from("<I", data, 56)[0]

        self._fat = self._read_fat()
        self._mini_fat = self._read_chain_array(
            struct.unpack_from("<I", data, 60)[0],
            struct.unpack_from("<I", data, 64)[0],
        )
        self.directory = self._read_directory(struct.unpack_from("<I", data, 48)[0])
        self._mini_stream: bytes | None = None

    # -- raw sector access ------------------------------------------------

    def _sector_offset(self, sector: int) -> int:
        # Sector 0 starts one sector-size in: the 512-byte header is padded out
        # to a full sector, so this is correct for 4096-byte sectors too.
        return (sector + 1) * self.sector_size

    def _sector(self, sector: int) -> bytes:
        start = self._sector_offset(sector)
        end = start + self.sector_size
        if start < 0 or end > len(self._data):
            raise CompoundFileError(f"sector {sector} out of range")
        return self._data[start:end]

    def _read_fat(self) -> list[int]:
        data = self._data
        fat_count = struct.unpack_from("<I", data, 44)[0]
        difat: list[int] = [
            struct.unpack_from("<I", data, 76 + 4 * i)[0]
            for i in range(min(109, max(0, fat_count)))
        ]
        # Additional DIFAT sectors chain through their own last entry.
        next_difat = struct.unpack_from("<I", data, 68)[0]
        difat_count = struct.unpack_from("<I", data, 72)[0]
        seen: set[int] = set()
        per_sector = self.sector_size // 4
        while (
            next_difat not in (_ENDOFCHAIN, _FREESECT)
            and next_difat not in seen
            and len(difat) < fat_count
            and len(seen) <= difat_count + 1
        ):
            seen.add(next_difat)
            block = self._sector(next_difat)
            for i in range(per_sector - 1):
                entry = struct.unpack_from("<I", block, 4 * i)[0]
                if entry in (_ENDOFCHAIN, _FREESECT):
                    continue
                difat.append(entry)
            next_difat = struct.unpack_from("<I", block, 4 * (per_sector - 1))[0]

        fat: list[int] = []
        for sector in difat:
            if sector in (_ENDOFCHAIN, _FREESECT):
                continue
            block = self._sector(sector)
            fat.extend(struct.unpack_from("<I", block, 4 * i)[0] for i in range(per_sector))
        if not fat:
            raise CompoundFileError("compound file has an empty FAT")
        return fat

    def _read_chain_array(self, first: int, count: int) -> list[int]:
        """Read a FAT-chained array of 32-bit entries (used for the mini FAT)."""
        out: list[int] = []
        per_sector = self.sector_size // 4
        for sector in self._chain(first, limit=max(count, 0) + 1):
            block = self._sector(sector)
            out.extend(struct.unpack_from("<I", block, 4 * i)[0] for i in range(per_sector))
        return out

    def _chain(self, first: int, limit: int | None = None) -> list[int]:
        """Sector chain starting at ``first``.

        A corrupt or hostile FAT can point a sector at itself or form a cycle;
        the visited set makes that terminate instead of spinning forever.
        """
        chain: list[int] = []
        visited: set[int] = set()
        sector = first
        cap = limit if limit is not None else len(self._fat) + 1
        while sector not in (_ENDOFCHAIN, _FREESECT, _DIFSECT, _FATSECT):
            if sector in visited or len(chain) >= cap or sector >= len(self._fat) or sector < 0:
                break
            visited.add(sector)
            chain.append(sector)
            sector = self._fat[sector]
        return chain

    # -- directory + streams ----------------------------------------------

    def _read_directory(self, first: int) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        for sector in self._chain(first):
            block = self._sector(sector)
            for offset in range(0, len(block), 128):
                raw = block[offset : offset + 128]
                if len(raw) < 128:
                    break
                name_len = struct.unpack_from("<H", raw, 64)[0]
                obj_type = raw[66]
                if obj_type not in (1, 2, 5):
                    continue
                name = raw[: max(0, min(64, name_len))].decode("utf-16-le", "ignore").rstrip("\x00")
                entries.append(
                    {
                        "name": name,
                        "type": obj_type,
                        "start": struct.unpack_from("<I", raw, 116)[0],
                        "size": struct.unpack_from("<Q", raw, 120)[0],
                    }
                )
        if not entries:
            raise CompoundFileError("compound file has no directory entries")
        return entries

    def _load_mini_stream(self) -> bytes:
        if self._mini_stream is None:
            root = next((e for e in self.directory if e["type"] == 5), None)
            if root is None:
                self._mini_stream = b""
            else:
                size = min(int(root["size"]), MAX_STREAM_BYTES)
                blocks = [self._sector(s) for s in self._chain(int(root["start"]))]
                self._mini_stream = b"".join(blocks)[:size]
        return self._mini_stream

    def names(self) -> list[str]:
        return [str(e["name"]) for e in self.directory]

    def read_stream(self, name: str) -> bytes | None:
        """Contents of the named stream, or ``None`` when absent/unreadable."""
        entry = next(
            (e for e in self.directory if e["type"] == 2 and str(e["name"]) == name),
            None,
        )
        if entry is None:
            return None
        size = int(entry["size"])
        if size <= 0:
            return b""
        if size > MAX_STREAM_BYTES:
            logger.warning("refusing oversized .doc stream %s (%d bytes)", name, size)
            return None
        start = int(entry["start"])
        try:
            if size < self.mini_cutoff:
                mini = self._load_mini_stream()
                pieces = []
                for sector in self._mini_chain(start, size):
                    offset = sector * self.mini_sector_size
                    pieces.append(mini[offset : offset + self.mini_sector_size])
                return b"".join(pieces)[:size]
            blocks = [self._sector(s) for s in self._chain(start)]
            return b"".join(blocks)[:size]
        except CompoundFileError as exc:
            logger.debug("stream %s unreadable: %s", name, exc)
            return None

    def _mini_chain(self, first: int, size: int) -> list[int]:
        needed = (size + self.mini_sector_size - 1) // self.mini_sector_size
        chain: list[int] = []
        visited: set[int] = set()
        sector = first
        while sector not in (_ENDOFCHAIN, _FREESECT) and len(chain) < needed:
            if sector in visited or sector < 0 or sector >= len(self._mini_fat):
                break
            visited.add(sector)
            chain.append(sector)
            sector = self._mini_fat[sector]
        return chain


def _fib_clx_location(word_stream: bytes) -> tuple[int, int, bool]:
    """``(fcClx, lcbClx, use_1table)`` from the File Information Block.

    The FIB is variable-length: three count fields (``csw``, ``cslw``,
    ``cbRgFcLcb``) describe the sizes of the arrays between the fixed 32-byte
    base and ``FibRgFcLcb``, so the offset of ``fcClx`` has to be *walked*
    rather than hard-coded. ``fcClx`` is pair index 33 of ``FibRgFcLcb``,
    which every version from Word 97 onwards keeps in place by extending the
    array rather than reordering it.
    """
    if len(word_stream) < 64:
        raise CompoundFileError("WordDocument stream too short for a FIB")
    ident = struct.unpack_from("<H", word_stream, 0)[0]
    if ident != 0xA5EC:
        raise CompoundFileError(f"unexpected FIB identifier 0x{ident:04X}")
    flags = struct.unpack_from("<H", word_stream, 10)[0]
    use_1table = bool((flags >> 9) & 1)

    pos = 32
    csw = struct.unpack_from("<H", word_stream, pos)[0]
    pos += 2 + csw * 2
    cslw = struct.unpack_from("<H", word_stream, pos)[0]
    pos += 2 + cslw * 4
    cb_rg_fc_lcb = struct.unpack_from("<H", word_stream, pos)[0]
    pos += 2
    if cb_rg_fc_lcb < 34:
        # Word 6.0/95 and other pre-97 layouts do not carry fcClx at this
        # index. Refusing is correct: reading index 33 anyway would point at
        # arbitrary bytes and yield confident garbage.
        raise CompoundFileError(f"pre-Word-97 FIB layout (cbRgFcLcb={cb_rg_fc_lcb}); unsupported")
    if pos + 34 * 8 > len(word_stream):
        raise CompoundFileError("FIB truncated before fcClx")
    fc_clx, lcb_clx = struct.unpack_from("<II", word_stream, pos + 33 * 8)
    return fc_clx, lcb_clx, use_1table


def _piece_table(clx: bytes) -> list[tuple[int, int, bool]]:
    """``(cp_start, fc, compressed)`` pieces plus a terminating CP.

    A CLX is a sequence of ``Prc`` blocks (tag ``0x01``, skipped — they carry
    formatting) followed by a ``Pcdt`` (tag ``0x02``) whose ``PlcPcd`` is
    ``n+1`` character positions followed by ``n`` 8-byte piece descriptors.
    """
    pos = 0
    while pos < len(clx):
        tag = clx[pos]
        if tag == 0x01:
            if pos + 3 > len(clx):
                break
            size = struct.unpack_from("<H", clx, pos + 1)[0]
            pos += 3 + size
            continue
        if tag == 0x02:
            if pos + 5 > len(clx):
                break
            lcb = struct.unpack_from("<I", clx, pos + 1)[0]
            body = clx[pos + 5 : pos + 5 + lcb]
            return _parse_plcpcd(body)
        # Unknown tag: the CLX is not shaped as documented, so stop rather
        # than resynchronise on a guess.
        break
    return []


def _parse_plcpcd(body: bytes) -> list[tuple[int, int, bool]]:
    if len(body) < 4 + 8:
        return []
    count = (len(body) - 4) // 12
    if count <= 0 or count > MAX_PIECES:
        return []
    cps = [struct.unpack_from("<I", body, 4 * i)[0] for i in range(count + 1)]
    pieces: list[tuple[int, int, bool]] = []
    base = 4 * (count + 1)
    for i in range(count):
        fc_raw = struct.unpack_from("<I", body, base + 8 * i + 2)[0]
        compressed = bool(fc_raw & 0x40000000)
        fc = fc_raw & 0x3FFFFFFF
        if compressed:
            # A compressed piece stores cp1252 bytes at fc/2.
            fc //= 2
        pieces.append((cps[i], fc, compressed))
    pieces.append((cps[count], 0, False))  # terminator carries the end CP
    return pieces


def _clean(raw: str) -> str:
    """Turn Word's in-text control characters into plain text.

    Field handling is the part that matters for output quality: the text
    between ``0x13`` and ``0x14`` is a field *instruction* (``HYPERLINK
    "http://…"``, ``PAGEREF _Toc123``) and must be dropped, while the text
    between ``0x14`` and ``0x15`` is the field *result* the reader sees and
    must be kept. Skipping this distinction is why naive extractors emit
    hyperlink machinery in the middle of sentences.
    """
    out: list[str] = []
    in_instruction = False
    for char in raw:
        code = ord(char)
        if code == _FIELD_BEGIN:
            in_instruction = True
            continue
        if code == _FIELD_SEPARATOR:
            in_instruction = False
            continue
        if code == _FIELD_END:
            in_instruction = False
            continue
        if in_instruction:
            continue
        if code in (_PARAGRAPH_END, _LINE_BREAK, _PAGE_BREAK):
            out.append("\n")
        elif code == _CELL_OR_ROW_END:
            out.append("\t")
        elif code == _NBSP:
            out.append(" ")
        elif code == _NB_HYPHEN:
            out.append("-")
        elif code == _OPTIONAL_HYPHEN:
            continue
        elif code in _DROP:
            continue
        else:
            out.append(char)
    return "".join(out)


def extract_doc_text(content: bytes) -> str:
    """Extract text from a Word 97-2003 ``.doc``.

    Returns ``""`` for anything it cannot read — a pre-Word-97 layout, a
    corrupt container, an encrypted document — rather than raising, so one bad
    file never aborts a sync.
    """
    try:
        cfb = CompoundFile(content)
    except CompoundFileError as exc:
        logger.debug("not a readable .doc container: %s", exc)
        return ""

    word_stream = cfb.read_stream("WordDocument")
    if not word_stream:
        logger.debug(".doc has no WordDocument stream")
        return ""

    try:
        fc_clx, lcb_clx, use_1table = _fib_clx_location(word_stream)
    except (CompoundFileError, struct.error) as exc:
        logger.debug(".doc FIB unreadable: %s", exc)
        return ""

    table = cfb.read_stream("1Table" if use_1table else "0Table")
    if table is None:
        # Some writers disagree with the flag; try the other stream before
        # giving up, since picking the wrong one is a known interop wart.
        table = cfb.read_stream("0Table" if use_1table else "1Table")
    if not table:
        logger.debug(".doc has no table stream")
        return ""

    clx = table[fc_clx : fc_clx + lcb_clx]
    if not clx:
        logger.debug(".doc CLX slice empty (fcClx=%d lcbClx=%d)", fc_clx, lcb_clx)
        return ""

    try:
        pieces = _piece_table(clx)
    except struct.error as exc:
        logger.debug(".doc piece table unreadable: %s", exc)
        return ""
    if len(pieces) < 2:
        logger.debug(".doc piece table empty")
        return ""

    return _clean(pieces_to_text(word_stream, pieces))


def pieces_to_text(word_stream: bytes, pieces: list[tuple[int, int, bool]]) -> str:
    """Concatenate the character data each piece points at, in document order.

    Split out from :func:`extract_doc_text` so the two decode branches can be
    tested independently of a container. That matters because the **compressed**
    branch is the one real Word exercises most (it stores plain ASCII/cp1252
    text single-byte, halving the offset) while LibreOffice-written files
    observed here always use UTF-16 pieces — so without a direct test the
    ``fc // 2`` arithmetic would ship unverified against the producer that
    actually depends on it.
    """
    chunks: list[str] = []
    total = 0
    for index in range(len(pieces) - 1):
        cp_start, fc, compressed = pieces[index]
        cch = pieces[index + 1][0] - cp_start
        if cch <= 0:
            continue
        if compressed:
            text = word_stream[fc : fc + cch].decode("cp1252", errors="replace")
        else:
            text = word_stream[fc : fc + cch * 2].decode("utf-16-le", errors="replace")
        chunks.append(text)
        total += len(text)
        if total >= MAX_TEXT_CHARS:
            logger.warning(".doc text exceeds %d chars; truncating", MAX_TEXT_CHARS)
            break
    return "".join(chunks)
