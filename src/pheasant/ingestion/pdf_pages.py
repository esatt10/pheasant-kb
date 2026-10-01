"""Reading a PDF's pages with pymupdf: the one implementation.

Split out of `extractor.py` when reading page *ranges* arrived: the native
extractor reads every page through :func:`pdf_page_texts`, and a preparation
worker reads one range of a long PDF through the same function
(`sync/pdf_split.py`), so a PDF split across a fleet is read by exactly the
code that reads it whole. Kept free of everything else in the extractor so
that claim is checkable by looking at one small module.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

# MuPDF's global caches are not safe to drive concurrently from many threads
# in one worker process.  Serialize native MuPDF access per process; horizontal
# worker replicas still extract separate documents in parallel.
_NATIVE_MUPDF_LOCK = threading.Lock()

#: ``(content, relative_path) -> one text per page``: where a PDF's page texts
#: come from. ``None`` means :func:`pdf_page_texts` here, in this process; the
#: sync engine supplies one that splits a long PDF across preparation workers
#: (`sync/pdf_split.py`). Either way the extractor joins and tidies the pages
#: itself, so the text it returns does not depend on who read them.
PdfPages = Callable[[bytes, str], list[str]]


def _pymupdf() -> Any | None:
    try:
        import pymupdf  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        try:
            import fitz as pymupdf  # type: ignore[import-not-found,no-redef]
        except ModuleNotFoundError:
            return None
    return pymupdf


def pdf_reader_version() -> str:
    """The pymupdf release that reads PDFs here, or ``""`` without one."""

    pymupdf = _pymupdf()
    return str(getattr(pymupdf, "__version__", "")) if pymupdf is not None else ""


def pdf_page_count(content: bytes) -> int:
    """Pages in a PDF, via pymupdf. Raises when pymupdf cannot open it."""

    pymupdf = _pymupdf()
    if pymupdf is None:
        raise ModuleNotFoundError("pymupdf")
    with _NATIVE_MUPDF_LOCK:
        with pymupdf.open(stream=content, filetype="pdf") as document:
            return int(document.page_count)


def pdf_page_texts(content: bytes, first: int = 0, stop: int | None = None) -> list[str]:
    """The text of pages ``[first, stop)``, one string per page, untidied.

    The one implementation of "read a PDF's pages": the native extractor reads
    all of them through it and a preparation worker reads its range through
    it, so a long PDF split across a fleet is read by exactly the code that
    reads it whole. Raises when pymupdf is missing or fails, which the native
    extractor turns into its builtin fallback.
    """

    pymupdf = _pymupdf()
    if pymupdf is None:
        raise ModuleNotFoundError("pymupdf")
    with _NATIVE_MUPDF_LOCK:
        with pymupdf.open(stream=content, filetype="pdf") as document:
            end = document.page_count if stop is None else min(stop, document.page_count)
            return [document[number].get_text() or "" for number in range(first, end)]
