"""Read one long PDF with the whole worker fleet.

Remote preparation parallelizes *across files*: a file is one task on one
worker. One 8,000-page PDF is therefore one task, read on one thread wherever
it lands. Extraction is where such a file's
time goes (pymupdf reads ~600 pages a second; section detection and chunking
the result take a fraction of a second), and it is the one step that does not
need the whole document, because a page's text depends only on that page.

So only that step is split. The indexer counts the pages, sends every worker
the same bytes and a different range -- the gRPC ``ExtractPages`` call or
``POST /internal/indexing/extract-pages``, whichever transport the fleet runs
-- and joins the answers in page order; tidying, section detection and
chunking stay on the indexer, because they need the whole document.

**The text is identical to reading the file locally**, and that is a
property of the construction rather than a hope. A worker reads its range
with :func:`~pheasant.ingestion.pdf_pages.pdf_page_texts`, the same function
the native extractor reads a whole PDF with, and returns pages untidied; a
worker on a different pymupdf release is refused rather than trusted. Every
range the fleet cannot read -- a worker down, a deadline, an old worker
answering UNIMPLEMENTED or 404 -- is read here by that same function. A PDF
pymupdf cannot read fails the same way both ways and takes the native
extractor's builtin fallback.

Sending whole bytes per range rather than carving a sub-PDF per range costs
network (bytes x ranges) and buys exactness: a carved document re-serializes
fonts and resources, and "probably the same text" is not the standard here.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pheasant.ingestion.pdf_pages import pdf_page_count, pdf_page_texts
from pheasant.sync.preparation import _remote_inflight_batches
from pheasant.sync.remote_worker import RemoteWorkerError, configured_token

logger = logging.getLogger(__name__)

_ATTACH_LOCK = threading.Lock()


class RemotePdfPages:
    """A ``PdfPages`` provider that reads long PDFs on the worker fleet."""

    def __init__(
        self,
        urls: list[str],
        token_env: str,
        *,
        pages_per_task: int,
        timeout: float,
        max_parallel_files: int = 1,
        max_inflight: int = 0,
        transport: str = "grpc",
        pool_factory: Any = None,
    ) -> None:
        self.urls = list(urls)
        self.token_env = token_env
        self.pages_per_task = max(1, int(pages_per_task))
        self.timeout = float(timeout)
        self.max_parallel_files = max(1, int(max_parallel_files or 1))
        self.max_inflight = int(max_inflight or 0)
        self.transport = transport
        self._pool_factory = pool_factory
        self._pool: Any = None
        self._lock = threading.Lock()

    def splits(self, content: bytes) -> bool:
        """Whether this PDF is long enough to read on the fleet."""

        try:
            return pdf_page_count(content) > self.pages_per_task
        except Exception:  # noqa: BLE001 - unreadable here is unreadable remotely
            return False

    def __call__(self, content: bytes, relative_path: str) -> list[str]:
        total = pdf_page_count(content)
        if total <= self.pages_per_task:
            return pdf_page_texts(content)
        try:
            pool = self._worker_pool()
        except RemoteWorkerError as exc:
            logger.warning("Reading %s locally: %s", relative_path, exc)
            return pdf_page_texts(content)
        ranges = [
            (first, min(first + self.pages_per_task, total))
            for first in range(0, total, self.pages_per_task)
        ]
        started = time.monotonic()
        # In flight exactly as many as remote preparation would keep: two per
        # URL unless `remote_worker_max_inflight_batches` says how many
        # replicas a load-balanced URL stands for.
        inflight = _remote_inflight_batches(
            self.max_parallel_files, len(ranges), len(self.urls), self.max_inflight
        )
        with ThreadPoolExecutor(
            max_workers=inflight,
            thread_name_prefix="pheasant-pdf-pages",
        ) as executor:
            parts = list(
                executor.map(lambda span: self._read(pool, content, relative_path, *span), ranges)
            )
        logger.info(
            "Read %d pages of %s in %d ranges on the worker fleet in %.1fs",
            total,
            relative_path,
            len(ranges),
            time.monotonic() - started,
        )
        return [page for part in parts for page in part]

    def _read(self, pool: Any, content: bytes, relative_path: str, first: int, stop: int):
        try:
            return pool.extract_pages(
                content, first, stop, deadline=time.monotonic() + self.timeout
            )
        except Exception as exc:  # noqa: BLE001 - the fleet is an optimization
            logger.warning(
                "Reading pages %d-%d of %s locally: %s", first + 1, stop, relative_path, exc
            )
            return pdf_page_texts(content, first, stop)

    def _worker_pool(self) -> Any:
        with self._lock:
            if self._pool is None:
                token = configured_token(self.token_env)
                if self._pool_factory is not None:
                    self._pool = self._pool_factory(self.urls, token)
                else:
                    from pheasant.sync.worker_pool import WorkerPool

                    # Its own pool, not the one remote preparation uses: a
                    # worker too old to know `ExtractPages` must not trip the
                    # breaker that decides whether it gets whole files.
                    self._pool = WorkerPool(
                        self.urls, token, timeout=self.timeout, transport_name=self.transport
                    )
            return self._pool

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()


def remote_pdf_pages(config: Any) -> RemotePdfPages | None:
    """The fleet's page reader, or ``None`` where this config has no fleet."""

    concurrency = config.sync.concurrency
    urls = list(concurrency.remote_worker_urls or [])
    pages_per_task = int(getattr(concurrency, "remote_worker_pdf_pages_per_task", 0) or 0)
    if str(concurrency.file_executor or "").lower() != "remote" or not urls or pages_per_task <= 0:
        return None
    return RemotePdfPages(
        urls,
        concurrency.remote_worker_token_env,
        pages_per_task=pages_per_task,
        timeout=float(concurrency.remote_worker_timeout_seconds or 120),
        max_parallel_files=int(concurrency.max_parallel_files or 1),
        max_inflight=int(getattr(concurrency, "remote_worker_max_inflight_batches", 0) or 0),
        transport=str(concurrency.worker_transport or "http").lower(),
    )


def attach_fleet_pages(extractor: Any, config: Any) -> Any:
    """Give a native/auto extractor the fleet's page reader, once.

    Builtin and sandboxed extractors have no ``pdf_pages`` and are left as
    they are -- the sandboxed one deliberately, since reading pages outside
    its guest would bypass the boundary it exists to draw.
    """

    if extractor is None or getattr(extractor, "pdf_pages", False) is not None:
        return extractor
    with _ATTACH_LOCK:
        if extractor.pdf_pages is None:
            extractor.pdf_pages = remote_pdf_pages(config)
    return extractor


def reads_locally(extractor: Any, relative_path: str, content: bytes) -> bool:
    """Whether a remote-preparation item should be parsed here instead.

    Remote preparation would send a long PDF whole to one worker. Parsed here,
    its pages go to every worker and only the cheap steps run locally.
    """

    provider = getattr(extractor, "pdf_pages", None)
    return (
        isinstance(provider, RemotePdfPages)
        and Path(relative_path).suffix.lower() == ".pdf"
        and provider.splits(content)
    )
