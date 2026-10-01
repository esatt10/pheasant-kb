"""One long PDF read by the whole worker fleet (`sync/pdf_split.py`).

Remote preparation parallelizes across files, so one 8,000-page PDF was one
task on one thread -- and on a taxonomy-enabled source, which remote
preparation refuses, it never reached the fleet at all. Its page ranges now
go to every worker while tidying, section detection and chunking stay with
the indexer.

The property every test here comes back to is the one that makes the split
safe to ship: the indexed text is **identical** to a local read, whatever the
fleet does -- answers, fails, is too old, or reads with a different pymupdf.
The gRPC tests run a real server on a loopback port, as `test_grpc_worker.py`
does, because the value is in whether the pages arrive.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.ingestion.extractor import AutoExtractor, BuiltinExtractor
from pheasant.ingestion.pdf_pages import pdf_page_texts
from pheasant.sync.pdf_split import (
    RemotePdfPages,
    attach_fleet_pages,
    reads_locally,
    remote_pdf_pages,
)
from pheasant.sync.worker_pool import AllWorkersFailed

TOKEN_ENV = "PHEASANT_INDEX_WORKER_TOKEN"


def _pdf(pages: int) -> bytes:
    import pymupdf

    document = pymupdf.open()
    for number in range(pages):
        page = document.new_page()
        lines = [f"{number // 3 + 1}.{number % 3 + 1} Section on page {number + 1}"]
        lines += [f"Transmission service clause {number}-{line}." for line in range(12)]
        page.insert_text((36, 48), "\n".join(lines), fontsize=9)
    try:
        return document.tobytes()
    finally:
        document.close()


class _FakePool:
    """Reads ranges in-process, refusing the ones it is told to."""

    def __init__(self, fail: set[int] | None = None) -> None:
        self.fail = fail or set()
        self.ranges: list[tuple[int, int]] = []

    def extract_pages(self, content: bytes, first: int, stop: int, *, deadline: Any = None):
        self.ranges.append((first, stop))
        if first in self.fail:
            raise AllWorkersFailed({"grpc://worker": "down"})
        return pdf_page_texts(content, first, stop)

    def close(self) -> None:
        pass


def _provider(pool: Any, pages_per_task: int = 7) -> RemotePdfPages:
    return RemotePdfPages(
        ["grpc://worker:8766"],
        TOKEN_ENV,
        pages_per_task=pages_per_task,
        timeout=30,
        max_parallel_files=4,
        pool_factory=lambda _urls, _token: pool,
    )


def test_split_ranges_cover_every_page_once_in_order(monkeypatch: Any) -> None:
    monkeypatch.setenv(TOKEN_ENV, "t")
    content = _pdf(30)
    pool = _FakePool()
    split = AutoExtractor(pdf_pages=_provider(pool)).extract(content, "tariff.pdf")
    assert split == AutoExtractor().extract(content, "tariff.pdf")
    assert sorted(pool.ranges) == [(0, 7), (7, 14), (14, 21), (21, 28), (28, 30)]


def test_ranges_the_fleet_cannot_read_are_read_locally(monkeypatch: Any) -> None:
    monkeypatch.setenv(TOKEN_ENV, "t")
    content = _pdf(30)
    pool = _FakePool(fail={7, 21})
    split = AutoExtractor(pdf_pages=_provider(pool)).extract(content, "tariff.pdf")
    assert split == AutoExtractor().extract(content, "tariff.pdf")


def test_a_short_pdf_never_reaches_the_fleet(monkeypatch: Any) -> None:
    monkeypatch.setenv(TOKEN_ENV, "t")
    content = _pdf(7)
    pool = _FakePool()
    provider = _provider(pool)
    assert AutoExtractor(pdf_pages=provider).extract(content, "short.pdf")
    assert pool.ranges == []
    assert not reads_locally(AutoExtractor(pdf_pages=provider), "short.pdf", content)


def test_no_token_reads_the_whole_pdf_locally(monkeypatch: Any) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    content = _pdf(30)
    pool = _FakePool()
    split = AutoExtractor(pdf_pages=_provider(pool)).extract(content, "tariff.pdf")
    assert split == AutoExtractor().extract(content, "tariff.pdf")
    assert pool.ranges == []


def test_an_unreadable_pdf_takes_the_same_fallback_either_way(monkeypatch: Any) -> None:
    monkeypatch.setenv(TOKEN_ENV, "t")
    garbage = b"%PDF-1.4\nnot really a pdf"
    pool = _FakePool()
    assert AutoExtractor(pdf_pages=_provider(pool)).extract(
        garbage, "x.pdf"
    ) == AutoExtractor().extract(garbage, "x.pdf")


def test_a_standalone_region_is_unchanged() -> None:
    """Rule 7: no fleet configured, nothing attached, nothing different."""

    config = PheasantConfig()
    assert remote_pdf_pages(config) is None
    extractor = attach_fleet_pages(AutoExtractor(), config)
    assert extractor.pdf_pages is None


@pytest.mark.parametrize(
    "change",
    [
        {"worker_transport": "http"},
        {"file_executor": "thread"},
        {"remote_worker_urls": []},
        {"remote_worker_pdf_pages_per_task": 0},
    ],
)
def test_only_a_grpc_fleet_gets_a_page_reader(change: dict[str, Any]) -> None:
    concurrency = {
        "file_executor": "remote",
        "worker_transport": "grpc",
        "remote_worker_urls": ["grpc://worker:8766"],
        **change,
    }
    config = PheasantConfig.model_validate({"sync": {"concurrency": concurrency}})
    assert remote_pdf_pages(config) is None


def test_extractors_without_a_page_reader_are_left_alone() -> None:
    config = PheasantConfig.model_validate(
        {
            "sync": {
                "concurrency": {
                    "file_executor": "remote",
                    "worker_transport": "grpc",
                    "remote_worker_urls": ["grpc://worker:8766"],
                }
            }
        }
    )
    builtin = BuiltinExtractor()
    assert attach_fleet_pages(builtin, config) is builtin
    assert not hasattr(builtin, "pdf_pages")
    auto = attach_fleet_pages(AutoExtractor(), config)
    assert isinstance(auto.pdf_pages, RemotePdfPages)
    provider = auto.pdf_pages
    assert attach_fleet_pages(auto, config).pdf_pages is provider


# --------------------------------------------------------------------------
# Over a real gRPC worker
# --------------------------------------------------------------------------


@pytest.fixture
def grpc_worker(tmp_path: Path, monkeypatch: Any):  # type: ignore[no-untyped-def]
    grpc = pytest.importorskip("grpc", reason="the [grpc] extra is optional")
    from concurrent.futures import ThreadPoolExecutor

    from pheasant.sync.grpc_worker import PreparationWorkerServicer, load_protos

    class CountingServicer(PreparationWorkerServicer):
        ranges: list[tuple[int, int]]
        batches = 0
        reader_version: str | None = None

        def ExtractPages(self, request: Any, context: Any):  # noqa: N802
            self.ranges.append((request.first_page, request.stop_page))
            response = super().ExtractPages(request, context)
            if self.reader_version is not None:
                response.reader_version = self.reader_version
            return response

        def PrepareBatch(self, request_iterator: Any, context: Any):  # noqa: N802
            self.batches += 1
            yield from super().PrepareBatch(request_iterator, context)

    config = PheasantConfig()
    config.sync.concurrency.remote_worker_enabled = True
    monkeypatch.setenv(TOKEN_ENV, "grpc-token")
    _pb2, pb2_grpc = load_protos()
    servicer = CountingServicer(config, version="test")
    servicer.ranges = []
    server = grpc.server(ThreadPoolExecutor(max_workers=4))
    pb2_grpc.add_PreparationWorkerServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    try:
        yield f"grpc://127.0.0.1:{port}", servicer
    finally:
        server.stop(grace=0).wait()


def _grpc_provider(url: str, pages_per_task: int = 7) -> RemotePdfPages:
    return RemotePdfPages(
        [url], TOKEN_ENV, pages_per_task=pages_per_task, timeout=30, max_parallel_files=4
    )


def test_pages_read_over_grpc_are_the_pages_read_here(grpc_worker: Any) -> None:
    url, servicer = grpc_worker
    content = _pdf(30)
    provider = _grpc_provider(url)
    try:
        split = AutoExtractor(pdf_pages=provider).extract(content, "tariff.pdf")
    finally:
        provider.close()
    assert split == AutoExtractor().extract(content, "tariff.pdf")
    assert sorted(servicer.ranges) == [(0, 7), (7, 14), (14, 21), (21, 28), (28, 30)]


def test_a_worker_on_another_pymupdf_is_not_trusted(grpc_worker: Any) -> None:
    from pheasant.sync.worker_pool import TaskRejected, WorkerPool

    url, servicer = grpc_worker
    servicer.reader_version = "0.0.0-elsewhere"
    content = _pdf(30)
    pool = WorkerPool([url], "grpc-token", timeout=30, transport_name="grpc")
    try:
        with pytest.raises(TaskRejected, match="pymupdf"):
            pool.extract_pages(content, 0, 7)
    finally:
        pool.close()
    provider = _grpc_provider(url)
    try:
        split = AutoExtractor(pdf_pages=provider).extract(content, "tariff.pdf")
    finally:
        provider.close()
    assert split == AutoExtractor().extract(content, "tariff.pdf")


def test_the_http_transport_reads_pages_locally() -> None:
    from pheasant.sync.worker_pool import TaskRejected, WorkerPool

    pool = WorkerPool(["http://worker:8765"], "t", transport_name="http")
    with pytest.raises(TaskRejected, match="page ranges"):
        pool.extract_pages(b"", 0, 1)


@pytest.mark.parametrize("taxonomy", [True, False])
def test_a_sync_reads_a_long_pdf_on_the_fleet_and_indexes_what_a_local_sync_does(
    tmp_path: Path, grpc_worker: Any, taxonomy: bool
) -> None:
    """With taxonomy on, remote preparation refuses the source; with it off,
    remote preparation would send the PDF whole to one worker. Either way its
    pages now go to the fleet and the indexed chunks do not move."""

    from pheasant.sync.engine import SyncEngine

    url, servicer = grpc_worker
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "tariff.pdf").write_bytes(_pdf(30))

    def chunks(state_name: str, fleet: bool) -> list[tuple[Any, ...]]:
        concurrency: dict[str, Any] = {"lock_timeout_seconds": 1}
        if fleet:
            concurrency.update(
                file_executor="remote",
                worker_transport="grpc",
                remote_worker_urls=[url],
                remote_worker_pdf_pages_per_task=7,
            )
        config = PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": "pdf-split",
                    "state_path": str(tmp_path / state_name),
                    "workspace_root": str(tmp_path),
                    "exports_path": str(tmp_path / f"{state_name}-exports"),
                },
                "storage": {"graph_snapshots": False},
                "sync": {"concurrency": concurrency},
                "sources": [
                    {
                        "name": "tariff",
                        "type": "document_folder",
                        "path": str(corpus),
                        "include": ["**/*.pdf"],
                        "taxonomy": {"enabled": taxonomy},
                    }
                ],
            }
        )
        engine = SyncEngine(config)
        try:
            result = engine.sync_source("tariff", "full")
            assert result.indexed_artifacts == 1
            return [
                tuple(row)
                for row in engine.state.rows(
                    "SELECT id, text, start_line, end_line, heading_path FROM chunks "
                    "WHERE artifact_id LIKE 'file:tariff:%' ORDER BY id"
                )
            ]
        finally:
            engine.close()

    over_fleet = chunks("fleet", fleet=True)
    assert sorted(servicer.ranges) == [(0, 7), (7, 14), (14, 21), (21, 28), (28, 30)]
    assert servicer.batches == 0, "the PDF went whole to one worker"
    locally = chunks("local", fleet=False)
    assert over_fleet == locally
    assert len(locally) > 1
    if taxonomy:
        assert any(row[4] for row in locally), "the fixture should exercise headings"
