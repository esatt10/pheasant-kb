"""DOCX text, read with the standard library: paragraphs, and their equations.

A ``.docx`` is a ZIP of XML; ``word/document.xml`` holds the body as ``w:t``
runs inside ``w:p`` paragraphs, tables included, so reading it needs only
``zipfile`` and ``xml.etree``. Split out of :mod:`pheasant.ingestion.extractor`
when equations arrived: an Office Math equation (``m:oMath``) is not a ``w:t``
run, and :func:`docx_paragraph_text` writes one as LaTeX through
:mod:`pheasant.ingestion.omml` for both DOCX readers, this one and
python-docx's.
"""

from __future__ import annotations

import logging
from typing import Any

from pheasant.ingestion.extractor import _DOCX_MAIN_NS, _tidy

logger = logging.getLogger(__name__)


def extract_docx_text(content: bytes, math: bool = False) -> str:
    """Pure-stdlib DOCX text extraction.

    Unlike the PDF path this is not a fidelity compromise: ``word/document.xml``
    stores the body text as ``<w:t>`` runs inside ``<w:p>`` paragraphs, so
    reading it with ``zipfile`` + ``xml.etree`` recovers the document text
    directly. Tables are included (their cells are ordinary paragraphs).

    ``math`` writes each Office Math equation as LaTeX
    (:mod:`pheasant.ingestion.omml`); off, equations are skipped as they
    always were and the text is byte-identical.
    """
    import io
    import xml.etree.ElementTree as ET
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                return ""
            xml_bytes = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        logger.debug("DOCX archive unreadable: %s", exc)
        return ""

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        logger.debug("DOCX document.xml unparseable: %s", exc)
        return ""

    lines: list[str] = []
    for paragraph in root.iter(f"{_DOCX_MAIN_NS}p"):
        if math:
            lines.append(docx_paragraph_text(paragraph))
            continue
        buf: list[str] = []
        for node in paragraph.iter():
            tag = node.tag
            if tag == f"{_DOCX_MAIN_NS}t":
                buf.append(node.text or "")
            elif tag == f"{_DOCX_MAIN_NS}tab":
                buf.append("\t")
            elif tag == f"{_DOCX_MAIN_NS}br":
                buf.append("\n")
        lines.append("".join(buf))
    return _tidy("\n".join(lines))


def docx_paragraph_text(paragraph: Any) -> str:
    """A ``w:p``'s text with its equations as LaTeX, in document order.

    The walk the builtin reader does (``w:t``, ``w:tab``, ``w:br``, nested
    paragraphs included), except that an ``m:oMath`` or ``m:oMathPara`` is
    written as ``$…$`` / ``$$…$$`` rather than descended into. Shared by
    both DOCX readers: it reads ``xml.etree`` and lxml elements alike.
    """
    from pheasant.ingestion.omml import OMATH, OMATH_PARA, math_text

    buf: list[str] = []
    stack = [paragraph]
    while stack:
        node = stack.pop()
        tag = node.tag
        if tag in (OMATH, OMATH_PARA):
            text = math_text(node)
            if tag == OMATH_PARA and text:
                text = "\n" + text + "\n"
            buf.append(text)
            continue
        if tag == f"{_DOCX_MAIN_NS}t":
            buf.append(node.text or "")
        elif tag == f"{_DOCX_MAIN_NS}tab":
            buf.append("\t")
        elif tag == f"{_DOCX_MAIN_NS}br":
            buf.append("\n")
        stack.extend(reversed([child for child in node if isinstance(child.tag, str)]))
    return "".join(buf)
