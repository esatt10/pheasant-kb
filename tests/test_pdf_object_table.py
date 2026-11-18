"""The builtin reader's object table: lexed when read, and the same values.

`read_document` lexed every object a PDF holds before anything read one, and a
math page reads its page tree, resources and fonts -- a few dozen of a paper's
thousands. That lexing was most of what a PDFium math page cost for the
builtin advances it borrows. These hold the two halves of making it lazy: what
nobody reads is never lexed, and what is read is exactly what the eager table
held, including the two corners the eager loop decided by parsing (an
object-stream member that parses to nothing, and a name spelled with ``#``).
"""

from __future__ import annotations

import random

from pheasant.ingestion.pdf_objects import (
    _OBJ_RE,
    MAX_OBJECTS,
    _object_headers,
    lex_pdf,
    pages,
    parse_values,
    read_document,
)


def _document() -> bytes:
    member_offsets = b"7 0 8 1 "  # /First is its length: 8
    members = b" 42"  # object 7 is " " (nothing), object 8 is "42"
    objstm = member_offsets + members
    return b"".join(
        [
            b"%PDF-1.7\n",
            b"1 0 obj << /Type /Cat#61log /Pages 2 0 R >> endobj\n",
            b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n",
            b"3 0 obj << /Type /Page /MediaBox [0 0 200 300] /Contents 4 0 R >> endobj\n",
            b"4 0 obj << /Length 5 >> stream\nBT ET\nendstream endobj\n",
            b"7 0 obj (seven) endobj\n",
            b"5 0 obj << /Type /ObjStm /N 2 /First 8 >> stream\n",
            objstm + b"\nendstream endobj\n",
            b"%%EOF\n",
        ]
    )


def test_an_empty_object_stream_member_keeps_the_definition_it_would_have_replaced() -> None:
    document = read_document(_document())
    assert document.objects[7].value == parse_values(lex_pdf(b"(seven)"))[0]
    assert document.objects[8].value == 42


def test_a_catalog_spelled_with_an_escape_is_still_the_catalog() -> None:
    found = pages(read_document(_document()))
    assert len(found) == 1 and found[0].height == 300.0
    assert found[0].contents == [b"BT ET"]  # cut to /Length, as before


def test_reading_the_pages_lexes_only_the_objects_they_read() -> None:
    seen: list[bytes] = []

    def counting(data: bytes):
        seen.append(bytes(data))
        return lex_pdf(data)

    document = read_document(_document(), counting)
    # Only the object stream is read to find its members.
    assert len(seen) == 2 and b"/ObjStm" in seen[0]
    pages(document)
    lexed = b"\n".join(seen)
    assert b"(seven)" not in lexed and b"42" not in lexed
    assert len(seen) < 2 + len(document.objects)


def test_finding_objects_backwards_from_obj_is_the_pattern_it_replaced() -> None:
    """Every `obj` read backwards gives exactly the pattern's matches: the
    pattern began with a lookbehind and was tried at every byte of a file."""

    pieces = [b"obj", b"obj ", b"objx", b"_obj", b"0", b"12", b"1234567", b"12345678",
              b"123456", b" ", b"\n", b"\r\n", b"\t", b"\x0b", b"\x0c", b"\x00", b"a",
              b"<<", b"endobj", b"9 0 obj", b"x9 0 obj"]  # fmt: skip
    rng = random.Random(7)
    for _ in range(20_000):
        data = b"".join(rng.choice(pieces) for _ in range(rng.randint(0, 20)))
        expected = [(int(m.group(1)), m.start(), m.end()) for m in _OBJ_RE.finditer(data)]
        assert _object_headers(data) == expected[:MAX_OBJECTS], data
    assert _object_headers(_document()) == [
        (int(m.group(1)), m.start(), m.end()) for m in _OBJ_RE.finditer(_document())
    ]
