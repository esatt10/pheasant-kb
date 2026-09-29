"""Uploading documents through the UI.

Acceptance:

1. An upload becomes a **real source** — the same connector → chunk → graph
   pipeline as everything else, searchable afterwards. There is no second
   ingestion path to keep in step.
2. Untrusted filenames cannot escape the upload directory, collide, or produce
   a path that is unusable on another platform.
3. A second upload into the same source adds to it rather than replacing it.
4. One bad file does not lose the good ones it was dropped with.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.api.uploads import safe_filename, store_upload, unique_path, upload_root


def _file(name: str, body: bytes = b"# Title\n\nSome indexable prose.\n"):
    return ("files", (name, io.BytesIO(body), "text/markdown"))


# ------------------------------------------------------------ filename safety


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("notes.md", "notes.md"),
        # Only the basename survives — a traversal cannot escape the directory.
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\evil.dll", "evil.dll"),
        ("/absolute/path/report.pdf", "report.pdf"),
        # A leading dot would create .env / .git, which the exclude list would
        # then silently skip anyway.
        (".env", "env"),
        ("  spaced  .md", "spaced  .md"),
        # Windows-illegal characters are replaced, not the whole name.
        ("re<port>:v2?.md", "re_port_v2_.md"),
        ("tab\tseparated.md", "tab_separated.md"),
        ("", "upload"),
        ("///", "upload"),
    ],
)
def test_filenames_are_reduced_to_one_safe_component(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


@pytest.mark.parametrize(
    "name",
    [
        "\u00dcbersicht.pdf",
        "\u4f1a\u8b70\u30e1\u30e2.md",
        "notes-caf\u00e9.md",
        "\u03a9-report.txt",
    ],
)
def test_non_ascii_filenames_are_kept(name: str) -> None:
    """An allowlist of [A-Za-z0-9._-] would mangle every non-English name.

    It turned "\u00dcbersicht.pdf" into "_bersicht.pdf" and "\u4f1a\u8b70\u30e1\u30e2.md" into
    "___.md", while defending against nothing the denylist does not.
    """
    import unicodedata

    assert safe_filename(name) == unicodedata.normalize("NFC", name)


def test_visually_identical_names_normalise_to_one_file() -> None:
    """NFD (e + combining acute) must not become a different file from NFC \u00e9."""
    nfd = "cafe\u0301.md"
    nfc = "caf\u00e9.md"
    assert nfd != nfc
    assert safe_filename(nfd) == safe_filename(nfc)


def test_the_length_cap_counts_bytes_not_characters() -> None:
    """Filesystem limits are in bytes, and one emoji is four of them."""
    name = safe_filename("\U0001f986" * 200 + ".md")
    assert len(name.encode("utf-8")) <= 180
    assert name.endswith(".md")


def test_windows_reserved_device_names_are_renamed() -> None:
    """`con.txt` is not creatable on Windows; a state dir must stay portable."""
    assert safe_filename("con.txt") == "con_file.txt"
    assert safe_filename("LPT1") == "LPT1_file"


def test_a_very_long_name_is_truncated_but_keeps_its_extension() -> None:
    name = safe_filename("x" * 400 + ".md")
    assert len(name) <= 180
    assert name.endswith(".md")


def test_uploading_the_same_name_twice_does_not_overwrite(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("first", encoding="utf-8")
    assert unique_path(tmp_path, "notes.md").name == "notes-2.md"


def test_a_file_over_the_limit_is_refused_before_it_is_written(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="over the"):
        store_upload(tmp_path, "big.md", b"x" * 2048, max_bytes=1024)
    assert list(tmp_path.iterdir()) == []


def test_an_empty_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="empty"):
        store_upload(tmp_path, "nothing.md", b"")


def test_upload_root_is_created_under_state(tmp_path: Path) -> None:
    root = upload_root(tmp_path, "my uploads")
    assert root.is_dir()
    assert root.parent.name == "uploads"
    assert root.parent.parent == tmp_path


# ------------------------------------------------------------------ the route


def test_uploaded_documents_become_a_searchable_source(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))

    response = client.post(
        "/sources/upload",
        files=[_file("quarterly.md", b"# Quarterly\n\nThe kestrel migration finished.\n")],
        data={"source_name": "uploads", "sync_now": "true", "wait": "true"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stored"][0]["filename"] == "quarterly.md"
    assert body["source_name"] == "uploads"

    # It is a real source, listed like any other.
    names = {source["name"] for source in client.get("/sources").json()}
    assert "uploads" in names

    # And it went through the ordinary pipeline, so it is searchable.
    hits = client.post("/search", json={"query": "kestrel migration", "mode": "text"}).json()
    assert "kestrel" in str(hits).lower()


def test_uploaded_zip_indexes_nested_supported_files(loaded_config, config_path: Path) -> None:
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("handbook/guides/overview.md", "# Overview\n\nThe cedar relay is ready.\n")
        archive.writestr("handbook/unsupported.bin", b"ignored")

    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    response = client.post(
        "/sources/upload",
        files=[("files", ("handbook.zip", io.BytesIO(archive_data.getvalue()), "application/zip"))],
        data={"source_name": "zip-uploads", "sync_now": "true", "wait": "true"},
    )

    assert response.status_code == 200, response.text
    hits = client.post("/search", json={"query": "cedar relay", "mode": "text"}).json()
    assert "handbook.zip/handbook/guides/overview.md" in str(hits)


def test_a_second_upload_adds_to_the_same_source(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    data = {"source_name": "uploads", "sync_now": "false", "wait": "true"}

    client.post("/sources/upload", files=[_file("one.md")], data=data)
    second = client.post("/sources/upload", files=[_file("two.md")], data=data)

    directory = Path(second.json()["path"])
    assert {p.name for p in directory.iterdir()} == {"one.md", "two.md"}
    # Still one source, not two.
    assert sum(1 for s in client.get("/sources").json() if s["name"] == "uploads") == 1


def test_one_rejected_file_does_not_lose_the_rest(loaded_config, config_path: Path) -> None:
    loaded_config.sync.limits.max_file_size_mb = 1
    client = TestClient(create_app(config=loaded_config, config_path=config_path))

    response = client.post(
        "/sources/upload",
        files=[
            _file("good.md", b"# Good\n\nReadable prose.\n"),
            _file("huge.md", b"x" * (2 * 1024 * 1024)),
        ],
        data={"source_name": "uploads", "sync_now": "false", "wait": "true"},
    )

    body = response.json()
    assert [item["filename"] for item in body["stored"]] == ["good.md"]
    assert body["rejected"][0]["filename"] == "huge.md"


def test_an_upload_with_no_usable_files_is_a_400(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    response = client.post(
        "/sources/upload",
        files=[_file("empty.md", b"")],
        data={"source_name": "uploads", "sync_now": "false", "wait": "true"},
    )
    assert response.status_code == 400


def test_a_read_only_landing_zone_refuses_legibly_rather_than_crashing(
    loaded_config, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only `/state` is a deployment fact, not a caller's mistake.

    Every non-``all`` role in the fleet mounts `/state` read-only, so the drop
    zone's first ``mkdir`` raises ``OSError`` there. Left bare that surfaces as
    a 500 with an errno in it, which reads as a crash and tells an operator
    nothing about the mount that caused it — the reported symptom this guard
    exists for. The refusal has to name the fix.
    """

    # Built first: `create_app` legitimately creates its state directories, and
    # patching before that simulates a region that never started rather than a
    # running replica whose landing zone is read-only.
    client = TestClient(create_app(config=loaded_config, config_path=config_path))

    def _read_only(*_args, **_kwargs):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", _read_only)

    response = client.post(
        "/sources/upload",
        files=[_file("blocked.md")],
        data={"source_name": "uploads", "sync_now": "false", "wait": "true"},
    )

    # 503, not 500: the region is telling an operator its deployment is wrong,
    # and not 4xx, because resubmitting a different file changes nothing.
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "Read-only file system" in detail
    assert "/state/uploads" in detail


def test_the_unwritable_landing_zone_refusal_is_in_the_public_code_table() -> None:
    """A refusal an agent cannot branch on is a refusal it retries forever.

    `ContaminationRefused` was once absent from the derived code table for
    exactly this reason, so a new code owes the same check.
    """

    from pheasant.services.errors import LandingZoneUnwritable

    refusal = LandingZoneUnwritable("/state/uploads/x", OSError(30, "Read-only file system"))
    assert refusal.code == "LANDING_ZONE_UNWRITABLE"
    assert refusal.status == 503
    # Not retryable: an operator has to change a mount first.
    assert refusal.retryable is False


def test_upload_with_wait_false_returns_a_job_to_follow(loaded_config, config_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    response = client.post(
        "/sources/upload",
        files=[_file("async.md")],
        data={"source_name": "uploads", "sync_now": "true", "wait": "false"},
    )
    body = response.json()
    assert body["syncing"] is True
    assert body["job_id"]
    assert client.get(f"/jobs/{body['job_id']}").status_code == 200


@pytest.mark.asyncio
async def test_a_wait_true_upload_offloads_its_blocking_work(
    loaded_config, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``engine.sync_source`` — and the disk write, source
    registry and audit log before it — used to run directly on the event
    loop for a ``wait=true`` upload, because the route was ``async def``
    with nothing offloading the blocking work inside it. Measured before
    the fix: one in-flight upload delayed a *concurrently issued* GET
    /ready to the same ~5s latency the upload itself took — a single
    upload stalled every other request in the process, not just uploads.

    Checked directly by *which thread* the blocking call actually runs on,
    rather than by racing two concurrent requests against one event loop
    and timing the result. That race is exactly the kind of interleaving a
    test cannot reliably control: a coroutine blocked on a genuinely
    synchronous call yields the loop to nothing at all, including this
    test's own polling or a competing request's dispatch, so an attempt to
    *observe* the block while it is happening is a race the test can lose
    even when the bug is real. Which thread ``engine.sync_source`` runs on
    has no such timing window — it is decided once, at the call, and stays
    true for the call's whole duration.
    """

    import threading
    import types

    from pheasant.sync.worker import WorkerBackedEngine

    app = create_app(config=loaded_config, config_path=config_path)

    calling_thread: list[int] = []

    def _recording_sync_source(*args, **kwargs):
        calling_thread.append(threading.get_ident())
        return types.SimpleNamespace(status="succeeded", indexed_artifacts=0, skipped_artifacts=0)

    # The scaled architecture deliberately executes wait=true syncs through
    # WorkerBackedEngine so CPU-bound indexing happens in a child process.
    # Replace that boundary here to observe the thread used by the upload
    # route without starting a real child process.
    monkeypatch.setattr(WorkerBackedEngine, "sync_source", _recording_sync_source)

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/sources/upload",
            files=[_file("slow.md")],
            data={"source_name": "uploads", "sync_now": "true", "wait": "true"},
        )

    assert response.status_code == 200
    assert calling_thread, "WorkerBackedEngine.sync_source was never called"
    assert calling_thread[0] != threading.get_ident(), (
        "WorkerBackedEngine.sync_source ran on the event loop's own thread, not a worker "
        "thread — a slow sync_source call here would block every other "
        "request in the process"
    )


def test_a_traversing_filename_lands_inside_the_upload_directory(
    loaded_config, config_path: Path
) -> None:
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    response = client.post(
        "/sources/upload",
        files=[_file("../../escaped.md")],
        data={"source_name": "uploads", "sync_now": "false", "wait": "true"},
    )
    body = response.json()
    stored = Path(body["stored"][0]["path"]).resolve()
    assert stored.parent == Path(body["path"]).resolve()
    assert stored.name == "escaped.md"
