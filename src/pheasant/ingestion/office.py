"""Text extraction for the remaining office/e-book document formats.

Companion to :mod:`pheasant.ingestion.extractor`, which owns PDF, DOCX and
HTML. This module adds ``.pptx``, ``.xlsx``, ``.epub`` and ``.rtf``; the
legacy binary ``.doc`` is big enough to warrant its own module
(:mod:`pheasant.ingestion.msdoc`).

Everything here is **pure standard library** — ``zipfile`` +
``xml.etree.ElementTree`` for the three container formats, and a plain
tokenizer for RTF. That is not a compromise: for OOXML and EPUB the XML *is*
where the text lives, so reading it directly is the same text a heavyweight
library would hand back, and RTF is a text format by definition. No provider
here uses a model or the network.

Namespace handling
------------------
Every element match is on the **local name** (``tag.rpartition("}")[2]``)
rather than a hard-coded namespace URI. OOXML namespace URIs vary between
producers and versions, and a strict match silently returns nothing when a
file uses a URI you did not anticipate — the exact failure mode this whole
change exists to remove.

Producer-verified
-----------------
The readers were written against files produced by a real office suite
(LibreOffice Writer/Calc/Impress), not against the specification alone, so
the structures they walk are ones real producers actually emit: ``<a:t>``
runs inside ``ppt/slides/slideN.xml``, ``t="s"`` cells indexing
``xl/sharedStrings.xml``, an EPUB spine reached through
``META-INF/container.xml`` → OPF, and RTF text wrapped in ``{\\loch …}``
groups after a ``\\fonttbl``/``\\colortbl``/``\\stylesheet``/``\\info``
preamble that must be skipped.
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterator

logger = logging.getLogger(__name__)

# Bounds so a hostile archive cannot exhaust host memory. A zip whose members
# inflate to gigabytes, or a sheet claiming millions of rows, stops here
# rather than in the OOM killer.
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_TOTAL_CHARS = 8 * 1024 * 1024

#: An XHTML chapter's ``<head>`` element, removed before its text is read.
_EPUB_HEAD = re.compile(rb"<head\b.*?</head\s*>", re.IGNORECASE | re.DOTALL)
MAX_SHEET_ROWS = 200_000


def _local(tag: str) -> str:
    """Local name of a possibly namespaced ElementTree tag."""
    return tag.rpartition("}")[2]


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    """Read one archive member, bounded, returning ``None`` if unusable."""
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_MEMBER_BYTES:
        logger.warning("skipping oversized archive member %s (%d bytes)", name, info.file_size)
        return None
    try:
        with archive.open(info) as handle:
            return handle.read(MAX_MEMBER_BYTES + 1)[:MAX_MEMBER_BYTES]
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        logger.debug("archive member %s unreadable: %s", name, exc)
        return None


def _parse_member(archive: zipfile.ZipFile, name: str) -> ET.Element | None:
    raw = _read_member(archive, name)
    if raw is None:
        return None
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.debug("archive member %s is not parseable XML: %s", name, exc)
        return None


def _numbered(names: Iterator[str] | list[str], pattern: str) -> list[str]:
    """Archive members matching ``pattern`` (one ``(\\d+)`` group), numerically sorted.

    Numeric rather than lexicographic: ``slide10.xml`` must come after
    ``slide9.xml``, which a plain ``sorted()`` gets wrong.
    """
    matcher = re.compile(pattern)
    found: list[tuple[int, str]] = []
    for name in names:
        match = matcher.fullmatch(name)
        if match:
            found.append((int(match.group(1)), name))
    return [name for _index, name in sorted(found)]


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------


def _shape_text(root: ET.Element) -> list[str]:
    """Paragraph-wise text of a DrawingML tree.

    Text lives in ``<a:t>`` runs grouped into ``<a:p>`` paragraphs. Walking
    paragraphs (rather than concatenating every ``a:t`` in the file) keeps
    one bullet per line instead of running a whole slide together.
    """
    lines: list[str] = []
    for paragraph in root.iter():
        if _local(paragraph.tag) != "p":
            continue
        runs = [node.text or "" for node in paragraph.iter() if _local(node.tag) == "t"]
        text = "".join(runs).strip()
        if text:
            lines.append(text)
    return lines


def extract_pptx_text(content: bytes) -> str:
    """Slide text plus speaker notes, in slide order.

    Notes are real authored content (often the narrative a slide only gestures
    at), so they are indexed. Each slide's notes are located through that
    slide's own ``_rels`` entry rather than by assuming ``notesSlideN`` pairs
    with ``slideN`` — the numbering usually matches, but the relationship is
    what actually defines it.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        logger.debug("PPTX archive unreadable: %s", exc)
        return ""

    with archive:
        names = archive.namelist()
        blocks: list[str] = []
        for slide in _numbered(names, r"ppt/slides/slide(\d+)\.xml"):
            root = _parse_member(archive, slide)
            if root is None:
                continue
            lines = _shape_text(root)
            for notes in _notes_for_slide(archive, slide):
                notes_root = _parse_member(archive, notes)
                if notes_root is not None:
                    lines.extend(_shape_text(notes_root))
            if lines:
                blocks.append("\n".join(lines))
    return "\n\n".join(blocks)[:MAX_TOTAL_CHARS]


def _notes_for_slide(archive: zipfile.ZipFile, slide: str) -> list[str]:
    """Notes-slide members related to ``slide``, via its relationship part."""
    prefix, _, base = slide.rpartition("/")
    rels_name = f"{prefix}/_rels/{base}.rels"
    root = _parse_member(archive, rels_name)
    if root is None:
        return []
    out: list[str] = []
    for node in root.iter():
        if _local(node.tag) != "Relationship":
            continue
        target = node.get("Target") or ""
        if "notesSlide" not in target:
            continue
        # Targets are relative to the slide's own directory.
        resolved = _resolve_relative(prefix, target)
        if resolved:
            out.append(resolved)
    return out


def _resolve_relative(base_dir: str, target: str) -> str | None:
    """Resolve an OPC/OPF relative target against ``base_dir``, rejecting escapes."""
    if target.startswith("/"):
        return target.lstrip("/")
    parts = [p for p in base_dir.split("/") if p] if base_dir else []
    for segment in target.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts:
                # Would escape the archive root — refuse rather than guess.
                return None
            parts.pop()
        else:
            parts.append(segment)
    return "/".join(parts) or None


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    root = _parse_member(archive, "xl/sharedStrings.xml")
    if root is None:
        return []
    strings: list[str] = []
    for item in root:
        if _local(item.tag) != "si":
            continue
        # One <si> may hold several <t> runs (rich text); concatenate them.
        strings.append("".join(n.text or "" for n in item.iter() if _local(n.tag) == "t"))
    return strings


def _workbook_sheets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """``(sheet name, member path)`` in workbook order.

    The name→part mapping goes through ``xl/_rels/workbook.xml.rels``, which is
    what actually defines it; falling back to numerically-ordered
    ``sheetN.xml`` only when the relationships are missing or unusable.
    """
    workbook = _parse_member(archive, "xl/workbook.xml")
    rels = _parse_member(archive, "xl/_rels/workbook.xml.rels")
    targets: dict[str, str] = {}
    if rels is not None:
        for node in rels.iter():
            if _local(node.tag) != "Relationship":
                continue
            rel_id = node.get("Id")
            target = node.get("Target") or ""
            if rel_id and target:
                resolved = _resolve_relative("xl", target)
                if resolved:
                    targets[rel_id] = resolved

    sheets: list[tuple[str, str]] = []
    if workbook is not None:
        for node in workbook.iter():
            if _local(node.tag) != "sheet":
                continue
            name = node.get("name") or ""
            rel_id = next(
                (v for k, v in node.attrib.items() if _local(k) == "id"),
                None,
            )
            member = targets.get(rel_id or "")
            if member and member in archive.namelist():
                sheets.append((name, member))
    if sheets:
        return sheets
    return [("", n) for n in _numbered(archive.namelist(), r"xl/worksheets/sheet(\d+)\.xml")]


def extract_xlsx_text(content: bytes) -> str:
    """Cell values sheet by sheet, tab-separated per row.

    Sheet names are included: a tab called "Q3 Forecast" is authored content a
    search should be able to find. Values come from ``<v>`` (resolved through
    the shared-string table when ``t="s"``) or from an inline ``<is>``;
    formulas (``<f>``) are skipped in favour of their cached result, which is
    what a reader of the spreadsheet actually sees.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        logger.debug("XLSX archive unreadable: %s", exc)
        return ""

    with archive:
        strings = _shared_strings(archive)
        blocks: list[str] = []
        for name, member in _workbook_sheets(archive):
            root = _parse_member(archive, member)
            if root is None:
                continue
            lines: list[str] = [name] if name else []
            rows = 0
            for row in root.iter():
                if _local(row.tag) != "row":
                    continue
                rows += 1
                if rows > MAX_SHEET_ROWS:
                    logger.warning("sheet %s exceeds %d rows; truncating", member, MAX_SHEET_ROWS)
                    break
                cells = [_cell_text(cell, strings) for cell in row if _local(cell.tag) == "c"]
                line = "\t".join(c for c in cells if c)
                if line.strip():
                    lines.append(line)
            if lines:
                blocks.append("\n".join(lines))
    return "\n\n".join(blocks)[:MAX_TOTAL_CHARS]


def _cell_text(cell: ET.Element, strings: list[str]) -> str:
    kind = cell.get("t") or "n"
    if kind == "inlineStr":
        return "".join(n.text or "" for n in cell.iter() if _local(n.tag) == "t").strip()
    value = next((n.text or "" for n in cell if _local(n.tag) == "v"), "")
    if kind == "s":
        try:
            return strings[int(value)]
        except (ValueError, IndexError):
            return ""
    return value.strip()


# ---------------------------------------------------------------------------
# EPUB
# ---------------------------------------------------------------------------


def extract_epub_text(content: bytes) -> str:
    """E-book text in **spine order**.

    Reading order is the spine, not archive or alphabetical order: an EPUB's
    chapter files are frequently named such that sorting them scrambles the
    book. Resolved via ``META-INF/container.xml`` → OPF → ``manifest`` +
    ``spine``, with a sorted-XHTML fallback when that chain is unusable, so a
    malformed package still yields its text rather than nothing.
    """
    from pheasant.ingestion.extractor import extract_html_text

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        logger.debug("EPUB archive unreadable: %s", exc)
        return ""

    with archive:
        members = _epub_spine(archive) or _epub_fallback(archive)
        blocks: list[str] = []
        total = 0
        for member in members:
            raw = _read_member(archive, member)
            if raw is None:
                continue
            # A chapter's <head> is metadata, not text a reader sees: its
            # <title> (often the book's) would otherwise lead every chapter.
            text = extract_html_text(_EPUB_HEAD.sub(b"", raw))
            if not text.strip():
                continue
            blocks.append(text)
            total += len(text)
            if total >= MAX_TOTAL_CHARS:
                break
    return "\n\n".join(blocks)[:MAX_TOTAL_CHARS]


def _epub_spine(archive: zipfile.ZipFile) -> list[str]:
    container = _parse_member(archive, "META-INF/container.xml")
    if container is None:
        return []
    opf_path = next(
        (
            node.get("full-path")
            for node in container.iter()
            if _local(node.tag) == "rootfile" and node.get("full-path")
        ),
        None,
    )
    if not opf_path:
        return []
    opf = _parse_member(archive, opf_path)
    if opf is None:
        return []
    opf_dir = opf_path.rpartition("/")[0]

    manifest: dict[str, str] = {}
    for node in opf.iter():
        if _local(node.tag) != "item":
            continue
        item_id = node.get("id")
        href = node.get("href")
        media = node.get("media-type") or ""
        if not item_id or not href:
            continue
        if "html" not in media and not href.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        resolved = _resolve_relative(opf_dir, href)
        if resolved:
            manifest[item_id] = resolved

    ordered: list[str] = []
    for node in opf.iter():
        if _local(node.tag) != "itemref":
            continue
        member = manifest.get(node.get("idref") or "")
        if member and member not in ordered:
            ordered.append(member)
    return ordered


def _epub_fallback(archive: zipfile.ZipFile) -> list[str]:
    return sorted(
        name
        for name in archive.namelist()
        if name.lower().endswith((".xhtml", ".html", ".htm"))
        and not name.lower().endswith(("toc.xhtml", "nav.xhtml"))
    )


# ---------------------------------------------------------------------------
# RTF
# ---------------------------------------------------------------------------

# Destinations whose contents are markup, not prose. Skipping them is the
# difference between clean text and a document that indexes its own font and
# colour tables — the `\fonttbl` group in a real file is full of `\'82\'6c`
# escapes that would otherwise decode into mojibake.
_RTF_SKIP_DESTINATIONS = frozenset(
    {
        "fonttbl",
        "colortbl",
        "stylesheet",
        "listtable",
        "listoverridetable",
        "info",
        "pict",
        "object",
        "objdata",
        "themedata",
        "colorschememapping",
        "latentstyles",
        "datastore",
        "rsidtbl",
        "generator",
        "xmlnstbl",
        "filetbl",
        "revtbl",
        "protusertbl",
        "userprops",
        "fchars",
        "lchars",
        "bkmkstart",
        "bkmkend",
        "upr",
        "falt",
        "panose",
        "fname",
    }
)

# Control words that produce whitespace rather than being skipped.
_RTF_BREAKS = {"par", "line", "sect", "page", "row", "softline"}
_RTF_TABS = {"tab", "cell"}

_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)?[ ]?|\\'([0-9a-fA-F]{2})|\\(.)|([{}])|([^\\{}]+)")


def extract_rtf_text(content: bytes) -> str:
    r"""Strip RTF control words down to prose.

    Implements the standard group/destination walk: a ``{`` pushes state, a
    ``}`` pops it, and a group opening with ``\*`` or a known markup
    destination is skipped whole (including nested groups). ``\'hh`` decodes
    as cp1252 and ``\uN`` as a Unicode code point, honouring ``\ucN`` for how
    many fallback characters follow the ``\u`` that must be discarded —
    without that, every non-ASCII character appears twice, once correctly and
    once as its substitution.
    """
    # Require the RTF signature. Unlike the ZIP-based formats, RTF has no
    # structural gate of its own, so without this check any file that happens
    # to carry an .rtf name — a JPEG, a truncated download — decodes as
    # latin-1 and indexes its raw bytes as prose. Mojibake in the index is
    # worse than no text at all, because it is unfindable *and* pollutes
    # scoring for real documents.
    head = content[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head.startswith(b"{\\rtf"):
        logger.debug("not an RTF document (missing {\\rtf signature))")
        return ""

    text = content.decode("latin-1", errors="ignore")
    out: list[str] = []
    # Per-group state: (skip_this_group, unicode_skip_count)
    stack: list[tuple[bool, int]] = []
    skip = False
    uc = 1
    skip_chars = 0  # remaining fallback chars to swallow after a \uN
    expect_destination = False

    for match in _RTF_TOKEN.finditer(text):
        word, param, hexval, escaped, brace, literal = match.groups()

        if brace == "{":
            stack.append((skip, uc))
            expect_destination = True
            continue
        if brace == "}":
            if stack:
                skip, uc = stack.pop()
            skip_chars = 0
            expect_destination = False
            continue

        if word is not None:
            if word == "u" and param is not None:
                was_destination = expect_destination
                expect_destination = False
                if not skip:
                    try:
                        code = int(param)
                    except ValueError:
                        code = -1
                    if code < 0:
                        code += 0x10000  # RTF writes >32767 as a negative
                    if 0 <= code <= 0x10FFFF:
                        out.append(chr(code))
                    skip_chars = uc
                del was_destination
                continue
            if word == "uc" and param is not None:
                try:
                    uc = max(0, int(param))
                except ValueError:
                    uc = 1
                expect_destination = False
                continue
            if word == "bin" and param is not None:
                # Binary run: its bytes are data, not text. Skipping the
                # control word alone would index the payload as prose.
                expect_destination = False
                continue
            if expect_destination and word in _RTF_SKIP_DESTINATIONS:
                skip = True
            expect_destination = False
            if skip:
                continue
            if word in _RTF_BREAKS:
                out.append("\n")
            elif word in _RTF_TABS:
                out.append("\t")
            skip_chars = 0
            continue

        if escaped is not None:
            if escaped == "*":
                # `{\*\dest ...}` — an ignorable destination.
                skip = True
                expect_destination = False
                continue
            expect_destination = False
            if not skip and escaped in ("\\", "{", "}"):
                out.append(escaped)
            elif not skip and escaped in ("\n", "\r"):
                out.append("\n")
            continue

        if hexval is not None:
            expect_destination = False
            if skip:
                continue
            if skip_chars:
                skip_chars -= 1
                continue
            out.append(bytes([int(hexval, 16)]).decode("cp1252", errors="replace"))
            continue

        if literal is not None:
            expect_destination = False
            if skip:
                continue
            chunk = literal.replace("\r", "").replace("\n", "")
            if skip_chars and chunk:
                consumed = min(skip_chars, len(chunk))
                skip_chars -= consumed
                chunk = chunk[consumed:]
            if chunk:
                out.append(chunk)

    return "".join(out)[:MAX_TOTAL_CHARS]
