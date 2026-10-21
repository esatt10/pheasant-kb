"""A PDF's objects, fonts and character maps, read with the standard library.

The builtin and sandboxed extractors read a PDF by scanning its content
streams for strings, which is enough for prose and not for an equation: an
equation is positions, sizes and fonts. Rebuilding one
(:mod:`pheasant.ingestion.pdf_math`) needs what a real reader resolves — which
font each ``/F12`` names, how its codes map to characters, how wide each glyph
is — and this module resolves exactly that, with no third-party import.

The lexer is the boundary
-------------------------
Everything here begins with :func:`lex_pdf`, one tokenizer for every PDF
syntax this module reads: object dictionaries, content streams, CMaps and
object streams. Its output is a flat list of typed tokens, and everything
after it walks those tokens, never the bytes. That is what lets the sandboxed
provider keep its guarantee: its WASM guest runs the same tokenizer
(``pdf_lex`` in the vendored crate, asserted byte-for-byte equal to
:func:`serialize_tokens` of this one), so attacker-controlled syntax is only
ever tokenized inside the fuel and memory cap. The host still finds object
boundaries and inflates streams, as it always has.

Bounded by construction: object and token counts, nesting depth, string and
CMap sizes all have caps, and anything malformed yields less, never an
exception into the sync.
"""

from __future__ import annotations

import re
import struct
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Token kinds. Small integers so the guest can emit them as one byte.
NUMBER, NAME, STRING, HEXSTRING, KEYWORD = 1, 2, 3, 4, 5
ARRAY_OPEN, ARRAY_CLOSE, DICT_OPEN, DICT_CLOSE, BRACE_OPEN, BRACE_CLOSE = 6, 7, 8, 9, 10, 11

MAX_TOKENS = 4_000_000
MAX_OBJECTS = 200_000
MAX_DEPTH = 32
MAX_INFLATED = 32 * 1024 * 1024

_WHITESPACE = b"\x00\t\n\x0c\r "
_DELIMITERS = b"()<>[]{}/%"
_NUMBER_RE = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")

Token = tuple[int, bytes]
Lexer = Callable[[bytes], list[Token]]


# ---------------------------------------------------------------------------
# The lexer (mirrored by the WASM guest)
# ---------------------------------------------------------------------------


def _literal_end(data: bytes, start: int) -> int:
    """Index of the ``)`` closing the literal that opened before ``start``."""

    depth = 1
    i = start
    n = len(data)
    while i < n:
        byte = data[i]
        if byte == 0x5C:
            i += 2
            continue
        if byte == 0x28:
            depth += 1
        elif byte == 0x29:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def lex_pdf(data: bytes) -> list[Token]:
    """Tokenize PDF syntax. The reference the sandbox guest is held to.

    An inline image's data (``ID`` … ``EI``) is skipped rather than lexed: it
    is binary, and lexing it would invent operators.
    """
    from pheasant.ingestion.extractor import _decode_pdf_literal

    tokens: list[Token] = []
    i = 0
    n = len(data)
    while i < n and len(tokens) < MAX_TOKENS:
        byte = data[i]
        if byte in _WHITESPACE:
            i += 1
        elif byte == 0x25:  # % comment
            while i < n and data[i] not in (10, 13):
                i += 1
        elif byte == 0x28:  # ( literal
            end = _literal_end(data, i + 1)
            tokens.append((STRING, _decode_pdf_literal(data[i + 1 : end])))
            i = end + 1
        elif byte == 0x3C:  # < hex or <<
            if i + 1 < n and data[i + 1] == 0x3C:
                tokens.append((DICT_OPEN, b""))
                i += 2
                continue
            end = data.find(b">", i + 1)
            if end == -1:
                end = n
            digits = bytes(c for c in data[i + 1 : end] if c in b"0123456789abcdefABCDEF")
            if len(digits) % 2:
                digits += b"0"
            tokens.append((HEXSTRING, bytes.fromhex(digits.decode("ascii"))))
            i = end + 1
        elif byte == 0x3E:  # >> or a stray >
            if i + 1 < n and data[i + 1] == 0x3E:
                tokens.append((DICT_CLOSE, b""))
                i += 2
            else:
                i += 1
        elif byte in b"[]{}":
            kind = {0x5B: ARRAY_OPEN, 0x5D: ARRAY_CLOSE, 0x7B: BRACE_OPEN, 0x7D: BRACE_CLOSE}[byte]
            tokens.append((kind, b""))
            i += 1
        elif byte == 0x2F:  # /name
            start = i + 1
            i = start
            while i < n and data[i] not in _WHITESPACE and data[i] not in _DELIMITERS:
                i += 1
            tokens.append((NAME, _decode_name(data[start:i])))
        elif byte == 0x29:  # stray )
            i += 1
        else:
            start = i
            while i < n and data[i] not in _WHITESPACE and data[i] not in _DELIMITERS:
                i += 1
            word = data[start:i]
            if _NUMBER_RE.fullmatch(word):
                tokens.append((NUMBER, word))
            else:
                tokens.append((KEYWORD, word))
                if word == b"ID":
                    i = _skip_inline_image(data, i)
    return tokens


def _decode_name(raw: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(raw):
        if raw[i] == 0x23 and i + 2 < len(raw) + 0 and _is_hex(raw[i + 1 : i + 3]):
            out.append(int(raw[i + 1 : i + 3], 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return bytes(out)


def _is_hex(pair: bytes) -> bool:
    return len(pair) == 2 and all(c in b"0123456789abcdefABCDEF" for c in pair)


def _skip_inline_image(data: bytes, i: int) -> int:
    """Past an inline image's data: the first ``EI`` with whitespace before it
    and whitespace, a delimiter or the end after it."""

    n = len(data)
    i += 1  # the single whitespace after ID
    while i < n:
        j = data.find(b"EI", i)
        if j == -1:
            return n
        before = data[j - 1] if j > 0 else 0x20
        after = data[j + 2] if j + 2 < n else 0x20
        if before in _WHITESPACE and (after in _WHITESPACE or after in _DELIMITERS):
            return j + 2
        i = j + 2
    return n


def serialize_tokens(tokens: list[Token]) -> bytes:
    """Tokens as the guest emits them: kind byte, u32 length, payload."""

    out = bytearray()
    for kind, value in tokens:
        out.append(kind)
        out += struct.pack("<I", len(value))
        out += value
    return bytes(out)


def deserialize_tokens(data: bytes) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    while i + 5 <= len(data):
        kind = data[i]
        (length,) = struct.unpack("<I", data[i + 1 : i + 5])
        tokens.append((kind, data[i + 5 : i + 5 + length]))
        i += 5 + length
    return tokens


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    number: int


class Name(bytes):
    """A PDF name, kept apart from a string of the same bytes."""


def parse_values(tokens: list[Token]) -> list[Any]:
    """Tokens as Python values: dicts, lists, Names, bytes, numbers, Refs.

    ``N G R`` becomes a :class:`Ref`; any other keyword is kept as ``str``.
    """
    pos = 0
    out: list[Any] = []

    def value(depth: int) -> Any:
        nonlocal pos
        kind, raw = tokens[pos]
        pos += 1
        if kind == NUMBER:
            return float(raw) if b"." in raw else int(raw)
        if kind == NAME:
            return Name(raw)
        if kind in (STRING, HEXSTRING):
            return bytes(raw)
        if kind == ARRAY_OPEN:
            items: list[Any] = []
            while pos < len(tokens) and tokens[pos][0] != ARRAY_CLOSE:
                if depth >= MAX_DEPTH:
                    pos += 1
                    continue
                items.append(value(depth + 1))
                _fold_refs(items)
            pos += 1
            return items
        if kind == DICT_OPEN:
            entries: list[Any] = []
            while pos < len(tokens) and tokens[pos][0] != DICT_CLOSE:
                if depth >= MAX_DEPTH:
                    pos += 1
                    continue
                entries.append(value(depth + 1))
                _fold_refs(entries)
            pos += 1
            result: dict[bytes, Any] = {}
            for index in range(0, len(entries) - 1, 2):
                if isinstance(entries[index], Name):
                    result[bytes(entries[index])] = entries[index + 1]
            return result
        return raw.decode("latin-1")

    while pos < len(tokens):
        out.append(value(0))
        _fold_refs(out)
    return out


def _fold_refs(items: list[Any]) -> None:
    if (
        len(items) >= 3
        and items[-1] == "R"
        and isinstance(items[-2], int)
        and isinstance(items[-3], int)
    ):
        del items[-2:]
        items[-1] = Ref(items[-1])


# ---------------------------------------------------------------------------
# The object table
# ---------------------------------------------------------------------------

_OBJ_RE = re.compile(rb"(?<![0-9])(\d{1,7})\s+(\d{1,5})\s+obj\b")


@dataclass
class PdfObject:
    value: Any
    stream: bytes | None = None


@dataclass
class PdfDocument:
    """Every object in a PDF, found by scanning, plus how to read streams."""

    objects: dict[int, PdfObject] = field(default_factory=dict)
    lexer: Lexer = lex_pdf

    def get(self, value: Any) -> Any:
        """``value`` with a reference followed (one level)."""

        seen = 0
        while isinstance(value, Ref) and seen < 16:
            entry = self.objects.get(value.number)
            value = entry.value if entry is not None else None
            seen += 1
        return value

    def stream(self, value: Any) -> bytes | None:
        """The decoded bytes of a referenced stream, or ``None``."""

        if isinstance(value, Ref):
            entry = self.objects.get(value.number)
            if entry is not None and entry.stream is not None:
                return _decode_stream(entry.value, entry.stream)
        return None


def _decode_stream(header: Any, raw: bytes) -> bytes | None:
    filters = header.get(b"Filter") if isinstance(header, dict) else None
    names = filters if isinstance(filters, list) else [filters] if filters else []
    data = raw
    for name in names:
        if bytes(name) in (b"FlateDecode", b"Fl"):
            try:
                data = zlib.decompressobj().decompress(data, MAX_INFLATED)
            except zlib.error:
                return None
        else:
            return None  # an image codec: not text
    return data


def read_document(content: bytes, lexer: Lexer = lex_pdf) -> PdfDocument:
    """Find every object by scanning for ``N G obj``, object streams included.

    Like the content-stream carving this module's neighbours do, scanning
    rather than following the cross-reference table means a damaged or
    incrementally updated xref does not stop the read; a later definition of
    an object number replaces an earlier one, as an incremental update does.
    """
    document = PdfDocument(lexer=lexer)
    matches = list(_OBJ_RE.finditer(content))[:MAX_OBJECTS]
    for index, match in enumerate(matches):
        number = int(match.group(1))
        start = match.end()
        limit = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        end = content.find(b"endobj", start, limit)
        body = content[start : end if end != -1 else limit]
        stream_at = re.search(rb"\bstream\r?\n", body)
        stream: bytes | None = None
        if stream_at is not None:
            raw = body[stream_at.end() :]
            cut = raw.rfind(b"endstream")
            stream = raw[:cut] if cut != -1 else raw
            body = body[: stream_at.start()]
        values = parse_values(lexer(body))
        value = values[0] if values else None
        if isinstance(value, dict) and stream is not None:
            length = value.get(b"Length")
            if isinstance(length, int) and 0 < length <= len(stream):
                stream = stream[:length]
        document.objects[number] = PdfObject(value, stream)
    _unpack_object_streams(document)
    return document


def _unpack_object_streams(document: PdfDocument) -> None:
    for entry in list(document.objects.values()):
        header = entry.value
        if not isinstance(header, dict) or header.get(b"Type") != b"ObjStm" or entry.stream is None:
            continue
        data = _decode_stream(header, entry.stream)
        first = header.get(b"First")
        count = header.get(b"N")
        if data is None or not isinstance(first, int) or not isinstance(count, int):
            continue
        offsets = parse_values(document.lexer(data[:first]))
        pairs = [
            (offsets[i], offsets[i + 1])
            for i in range(0, min(len(offsets), 2 * count) - 1, 2)
            if isinstance(offsets[i], int) and isinstance(offsets[i + 1], int)
        ]
        for index, (number, offset) in enumerate(pairs):
            if number in document.objects and document.objects[number].stream is not None:
                continue
            stop = pairs[index + 1][1] if index + 1 < len(pairs) else len(data) - first
            values = parse_values(document.lexer(data[first + offset : first + stop]))
            if values:
                document.objects[number] = PdfObject(values[0])


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@dataclass
class Page:
    contents: list[bytes]
    resources: dict
    height: float


def pages(document: PdfDocument) -> list[Page]:
    """The document's pages in order, through the page tree."""

    catalog = next(
        (e.value for e in document.objects.values() if isinstance(e.value, dict)
         and e.value.get(b"Type") == b"Catalog"),
        None,
    )  # fmt: skip
    out: list[Page] = []
    if catalog is None:
        return out

    def walk(node: Any, inherited: dict, depth: int) -> None:
        node = document.get(node)
        if not isinstance(node, dict) or depth > MAX_DEPTH or len(out) > 100_000:
            return
        attrs = dict(inherited)
        for key in (b"Resources", b"MediaBox"):
            if key in node:
                attrs[key] = node[key]
        kind = node.get(b"Type")
        if kind == b"Pages" or b"Kids" in node:
            for kid in document.get(node.get(b"Kids")) or []:
                walk(kid, attrs, depth + 1)
            return
        refs = document.get(node.get(b"Contents"))
        refs = refs if isinstance(refs, list) else [node.get(b"Contents")]
        contents = [data for data in (document.stream(ref) for ref in refs) if data]
        box = document.get(attrs.get(b"MediaBox")) or [0, 0, 612, 792]
        height = float(box[3]) if len(box) == 4 and isinstance(box[3], (int, float)) else 792.0
        resources = document.get(attrs.get(b"Resources"))
        out.append(Page(contents, resources if isinstance(resources, dict) else {}, height))

    walk(catalog.get(b"Pages"), {}, 0)
    return out


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------

_STANDARD = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$",
    "percent": "%", "ampersand": "&", "quotesingle": "'", "quoteright": "’",
    "quoteleft": "‘", "parenleft": "(", "parenright": ")", "asterisk": "*", "plus": "+",
    "comma": ",", "hyphen": "-", "period": ".", "slash": "/", "colon": ":", "semicolon": ";",
    "less": "<", "equal": "=", "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}",
    "asciitilde": "~", "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "fi": "fi", "fl": "fl",
    "ff": "ff", "ffi": "ffi", "ffl": "ffl", "endash": "–", "emdash": "—",
    "quotedblleft": "“", "quotedblright": "”", "bullet": "•",
    "periodcentered": "·", "degree": "°", "minus": "−", "multiply": "×",
    "divide": "÷", "plusminus": "±", "infinity": "∞", "partialdiff": "∂",
    "summation": "∑", "product": "∏", "integral": "∫", "radical": "√",
    "lessequal": "≤", "greaterequal": "≥", "notequal": "≠",
    "approxequal": "≈", "equivalence": "≡", "element": "∈", "nabla": "∇",
    "gradient": "∇", "arrowright": "→", "arrowleft": "←", "ellipsis": "…",
    "prime": "′", "macron": "¯", "dotaccent": "˙", "circumflex": "ˆ",
    "tilde": "˜", "dieresis": "¨", "acute": "´", "caron": "ˇ",
    "breve": "˘", "dotlessi": "ı", "dotlessj": "ȷ",
}  # fmt: skip
_GREEK_NAMES = (
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho"
    " sigma tau upsilon phi chi psi omega"
)


def glyph_name_to_unicode(name: str) -> str | None:
    """A glyph name as text: the common Adobe names, ``uniXXXX`` and ``uXXXX``."""

    if len(name) == 1 and name.isalpha():
        return name
    if name in _STANDARD:
        return _STANDARD[name]
    greek = _GREEK_NAMES.split()
    if name in greek:
        return chr(0x3B1 + greek.index(name) + (1 if greek.index(name) >= 17 else 0))
    if name[:1].isupper() and name.lower() in greek:
        index = greek.index(name.lower())
        return chr(0x391 + index + (1 if index >= 17 else 0))
    match = re.fullmatch(r"uni([0-9A-Fa-f]{4})+", name)
    if match:
        return "".join(chr(int(name[i : i + 4], 16)) for i in range(3, len(name), 4))
    match = re.fullmatch(r"u([0-9A-Fa-f]{4,6})", name)
    if match:
        code = int(match.group(1), 16)
        return chr(code) if code < 0x110000 else None
    return None


@dataclass
class Font:
    """What text extraction needs of one font resource."""

    name: str
    two_byte: bool = False
    #: code -> text, from ToUnicode, or Differences through glyph names.
    unicode: dict[int, str] = field(default_factory=dict)
    #: The font is TeX-encoded with no ToUnicode: show its codes as codes.
    tex_codes: bool = False
    widths: dict[int, float] = field(default_factory=dict)
    default_width: float = 500.0
    #: Glyph space to text space (1/1000 except for Type 3).
    scale: float = 0.001

    def decode(self, data: bytes) -> list[tuple[int, str]]:
        """``data`` as (code, text) pairs, one per glyph shown."""

        out: list[tuple[int, str]] = []
        if self.two_byte:
            for i in range(0, len(data) - 1, 2):
                code = data[i] << 8 | data[i + 1]
                out.append((code, self.unicode.get(code, "�")))
            return out
        for code in data:
            if code in self.unicode:
                out.append((code, self.unicode[code]))
            elif self.tex_codes:
                out.append((code, chr(code)))
            else:
                out.append((code, bytes([code]).decode("cp1252", errors="replace")))
        return out

    def width(self, code: int) -> float:
        return self.widths.get(code, self.default_width) * self.scale


def read_font(document: PdfDocument, value: Any) -> Font:
    from pheasant.ingestion.tex_symbols import base_font_name, tex_font_family

    font = document.get(value)
    if not isinstance(font, dict):
        return Font(name="")
    name = bytes(font.get(b"BaseFont") or font.get(b"Name") or b"").decode("latin-1", "replace")
    subtype = font.get(b"Subtype")
    result = Font(name=name)
    if subtype == b"Type0":
        result.two_byte = True
        descendants = document.get(font.get(b"DescendantFonts")) or []
        descendant = document.get(descendants[0]) if descendants else None
        if isinstance(descendant, dict):
            result.default_width = float(document.get(descendant.get(b"DW")) or 1000)
            result.widths = _cid_widths(document, document.get(descendant.get(b"W")))
    else:
        first = document.get(font.get(b"FirstChar"))
        widths = document.get(font.get(b"Widths"))
        if isinstance(first, int) and isinstance(widths, list):
            result.widths = {
                first + i: float(document.get(w)) for i, w in enumerate(widths)
                if isinstance(document.get(w), (int, float))
            }  # fmt: skip
        if subtype == b"Type3":
            matrix = document.get(font.get(b"FontMatrix"))
            if isinstance(matrix, list) and matrix and isinstance(matrix[0], (int, float)):
                result.scale = float(matrix[0])
        encoding = document.get(font.get(b"Encoding"))
        if isinstance(encoding, dict):
            differences = document.get(encoding.get(b"Differences")) or []
            code = 0
            for item in differences:
                if isinstance(item, int):
                    code = item
                elif isinstance(item, Name):
                    text = glyph_name_to_unicode(bytes(item).decode("latin-1"))
                    if text is not None:
                        result.unicode[code] = text
                    code += 1
    to_unicode = document.stream(font.get(b"ToUnicode"))
    if to_unicode:
        result.unicode.update(parse_cmap(to_unicode, document.lexer))
    if not to_unicode and tex_font_family(base_font_name(name)) is not None and not result.two_byte:
        result.tex_codes = True
        result.unicode.clear()  # the code is the truth; names are for printing
    return result


def _cid_widths(document: PdfDocument, entries: Any) -> dict[int, float]:
    widths: dict[int, float] = {}
    if not isinstance(entries, list):
        return widths
    i = 0
    while i < len(entries) and len(widths) < 70_000:
        start = document.get(entries[i])
        following = document.get(entries[i + 1]) if i + 1 < len(entries) else None
        if isinstance(start, int) and isinstance(following, list):
            for offset, width in enumerate(following):
                if isinstance(document.get(width), (int, float)):
                    widths[start + offset] = float(document.get(width))
            i += 2
        elif isinstance(start, int) and i + 2 < len(entries):
            stop, width = document.get(following), document.get(entries[i + 2])
            if isinstance(stop, int) and isinstance(width, (int, float)):
                for code in range(start, min(stop, start + 70_000) + 1):
                    widths[code] = float(width)
            i += 3
        else:
            i += 1
    return widths


def parse_cmap(data: bytes, lexer: Lexer = lex_pdf) -> dict[int, str]:
    """A ToUnicode CMap's ``bfchar`` and ``bfrange`` entries."""

    tokens = lexer(data[: 4 * 1024 * 1024])
    mapping: dict[int, str] = {}
    i = 0
    while i < len(tokens) and len(mapping) < 200_000:
        kind, raw = tokens[i]
        if kind == KEYWORD and raw == b"beginbfchar":
            i += 1
            while i + 1 < len(tokens) and tokens[i][0] != KEYWORD:
                source, target = tokens[i], tokens[i + 1]
                if source[0] == HEXSTRING and target[0] == HEXSTRING:
                    mapping[int.from_bytes(source[1], "big")] = _utf16(target[1])
                i += 2
            continue
        if kind == KEYWORD and raw == b"beginbfrange":
            i += 1
            while i + 2 < len(tokens) and tokens[i][0] != KEYWORD:
                low, high = tokens[i], tokens[i + 1]
                if low[0] != HEXSTRING or high[0] != HEXSTRING:
                    i += 1
                    continue
                start, stop = int.from_bytes(low[1], "big"), int.from_bytes(high[1], "big")
                stop = min(stop, start + 0xFFFF)
                if tokens[i + 2][0] == HEXSTRING:
                    base = tokens[i + 2][1]
                    for offset in range(stop - start + 1):
                        mapping[start + offset] = _utf16(
                            base[:-2]
                            + (int.from_bytes(base[-2:], "big") + offset).to_bytes(2, "big")
                            if len(base) >= 2
                            else base
                        )
                    i += 3
                elif tokens[i + 2][0] == ARRAY_OPEN:
                    j = i + 3
                    offset = 0
                    while j < len(tokens) and tokens[j][0] != ARRAY_CLOSE:
                        if tokens[j][0] == HEXSTRING:
                            mapping[start + offset] = _utf16(tokens[j][1])
                            offset += 1
                        j += 1
                    i = j + 1
                else:
                    i += 3
            continue
        i += 1
    return mapping


def _utf16(raw: bytes) -> str:
    try:
        return raw.decode("utf-16-be")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def page_fonts(document: PdfDocument, resources: dict, cache: dict[Any, Font]) -> dict[bytes, Font]:
    fonts = document.get(resources.get(b"Font"))
    out: dict[bytes, Font] = {}
    if not isinstance(fonts, dict):
        return out
    for key, value in fonts.items():
        cache_key = value.number if isinstance(value, Ref) else id(value)
        if cache_key not in cache:
            cache[cache_key] = read_font(document, value)
        out[key] = cache[cache_key]
    return out
