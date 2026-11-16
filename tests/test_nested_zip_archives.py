"""A ZIP inside a ZIP is a folder too.

An upload of ``Remarkable_and_misc.zip`` holding a further archive came out as
a README and two PDFs: the inner archive's extension is not an indexed type,
so it was filtered out with every other unsupported member, and the sync
reported itself healthy. What must hold now: nested archives are expanded
(bounded in depth, size and entries), their members index and read back like
any other member, an archive that cannot be opened is reported unreadable with
its reason, and the listing says what it left out.
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence import archive as archive_rows
from pheasant.persistence.state_store import StateStore
from pheasant.sync import zip_archive
from pheasant.sync.connectors import FilesystemConnector
from pheasant.sync.engine import SyncEngine
from pheasant.sync.zip_archive import ArchiveError, read_member, scan


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _upload(tmp_path: Path) -> Path:
    """An outer archive with a README, a folder of notes and two nested archives."""

    inner_most = _zip({"deep.md": b"# Deep\n\nThree archives down.\n"})
    notebooks = _zip(
        {
            "Notebook one.md": b"# Notebook one\n\nPheasants roost in trees.\n",
            "pages/page-1.md": b"# Page 1\n\nQuail nest on the ground.\n",
            "raw/0001.rm": b"\x00binary stroke data",
            "deeper.zip": inner_most,
        }
    )
    folder = tmp_path / "ws" / "uploads"
    folder.mkdir(parents=True)
    path = folder / "Remarkable_and_misc.zip"
    path.write_bytes(
        _zip(
            {
                "README.md": b"# Remarkable and misc\n\nNotebooks are in notebooks.zip.\n",
                "files/notes.md": b"# Notes\n\nGrouse drum in spring.\n",
                "files/notebooks.zip": notebooks,
                "files/broken.zip": b"this is not an archive",
                "files/sketch.rm": b"\x00more strokes",
            }
        )
    )
    return path


def _connector(tmp_path: Path) -> FilesystemConnector:
    config = PheasantConfig.model_validate(
        {
            "pheasant": {"defaults": "standalone"},
            "sources": [
                {
                    "name": "uploads",
                    "type": "document_folder",
                    "path": str(tmp_path / "ws" / "uploads"),
                    "include": ["**/*.md", "**/*.pdf"],
                }
            ],
        }
    )
    return FilesystemConnector(
        config.effective_source(config.sources[0]), StateStore(tmp_path / "s.db")
    )


def test_nested_archives_are_folders_and_read_back(tmp_path: Path) -> None:
    archive = _upload(tmp_path)
    listing = scan(archive)
    names = [name for name, _ in listing.members]
    assert names == [
        "README.md",
        "files/broken.zip",
        "files/notebooks.zip/Notebook one.md",
        "files/notebooks.zip/deeper.zip/deep.md",
        "files/notebooks.zip/pages/page-1.md",
        "files/notes.md",
    ]
    assert listing.nested == 2
    assert dict(listing.skipped) == {".rm": 2}
    assert [name for name, _ in listing.unreadable] == ["files/broken.zip"]

    info = dict(listing.members)["files/notebooks.zip/deeper.zip/deep.md"]
    assert info.filename == "files/notebooks.zip//deeper.zip//deep.md"
    assert read_member(archive, info.filename, 1_000_000) == b"# Deep\n\nThree archives down.\n"
    assert zip_archive.find_member(archive, "files/notebooks.zip/pages/page-1.md") is not None
    assert zip_archive.find_member(archive, "files/broken.zip") is None


def test_the_connector_lists_nested_members_and_reports_what_it_cannot_open(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _upload(tmp_path)
    connector = _connector(tmp_path)
    with caplog.at_level(logging.WARNING, logger="pheasant.sync.connectors"):
        items = {item.relative_path: item for item in connector.list_items()}
    assert set(items) == {
        "Remarkable_and_misc.zip/README.md",
        "Remarkable_and_misc.zip/files/notes.md",
        "Remarkable_and_misc.zip/files/notebooks.zip/Notebook one.md",
        "Remarkable_and_misc.zip/files/notebooks.zip/pages/page-1.md",
        "Remarkable_and_misc.zip/files/notebooks.zip/deeper.zip/deep.md",
        "Remarkable_and_misc.zip/files/broken.zip",
    }
    read = connector.read_item(items["Remarkable_and_misc.zip/files/notebooks.zip/Notebook one.md"])
    assert read.content.startswith(b"# Notebook one")
    with pytest.raises(ArchiveError, match="broken.zip: cannot open the nested archive"):
        connector.read_item(items["Remarkable_and_misc.zip/files/broken.zip"])
    summary = [
        r.getMessage()
        for r in caplog.records
        if "archive Remarkable_and_misc.zip" in r.getMessage()
    ]
    assert summary and "2 nested archive(s) opened, 1 could not be opened" in summary[0]
    assert "'.rm': 2" in summary[0]


def test_depth_and_damage_are_bounded_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _upload(tmp_path)
    monkeypatch.setattr(zip_archive, "MAX_ARCHIVE_DEPTH", 2)
    listing = scan(archive)
    unreadable = dict(listing.unreadable)
    assert "files/notebooks.zip/deeper.zip" in unreadable
    assert "deep" in str(unreadable["files/notebooks.zip/deeper.zip"].error)
    assert "files/notebooks.zip/pages/page-1.md" in dict(listing.members)

    # A damaged outer archive is one unreadable item, not a failed listing.
    (tmp_path / "ws" / "uploads" / "torn.zip").write_bytes(b"PK\x03\x04 torn")
    monkeypatch.setattr(zip_archive, "MAX_ARCHIVE_DEPTH", 4)
    items = {item.relative_path: item for item in _connector(tmp_path).list_items()}
    assert "torn.zip" in items
    with pytest.raises(ArchiveError, match="torn.zip"):
        _connector(tmp_path).read_item(items["torn.zip"])


def test_a_sync_indexes_nested_members_and_names_the_unreadable_one(tmp_path: Path) -> None:
    _upload(tmp_path)
    for name in ("state", "exports"):
        (tmp_path / name).mkdir()
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "kb",
                "defaults": "standalone",
                "workspace_root": str(tmp_path / "ws"),
                "state_path": str(tmp_path / "state"),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [
                {
                    "name": "uploads",
                    "type": "document_folder",
                    "path": "uploads",
                    "include": ["**/*.md"],
                }
            ],
        }
    )
    engine = SyncEngine(config)
    try:
        result = engine.sync_source("uploads")
        paths = {
            str(dict(r)["relative_path"])
            for r in engine.state.rows("SELECT relative_path FROM artifacts")
        }
        assert {
            "Remarkable_and_misc.zip/files/notebooks.zip/Notebook one.md",
            "Remarkable_and_misc.zip/files/notebooks.zip/pages/page-1.md",
            "Remarkable_and_misc.zip/files/notebooks.zip/deeper.zip/deep.md",
        } <= paths
        assert "Remarkable_and_misc.zip/files/broken.zip" not in paths
        assert result.status != "failed"
        reported = str(result.details)
        assert "broken.zip" in reported and "cannot open the nested archive" in reported
        # What the archive held is in the sync's own record, not only a log line:
        # on a fleet the sync runs in a child whose INFO lines reach no log.
        (summary,) = result.details["archives"]
        assert summary["archive"] == "Remarkable_and_misc.zip"
        assert summary["nested_archives"] == 2
        assert summary["could_not_open"] == ["files/broken.zip"]
        assert summary["skipped_by_type"] == {".rm": 2}

        # An unchanged re-sync reads nothing new and keeps the same documents.
        again = engine.sync_source("uploads")
        assert again.status != "failed"
        assert {
            str(dict(r)["relative_path"])
            for r in engine.state.rows("SELECT relative_path FROM artifacts")
        } == paths
    finally:
        engine.close()


def _upload_engine(tmp_path: Path) -> SyncEngine:
    state = tmp_path / "state"
    root = state / "uploads" / "uploads"
    root.mkdir(parents=True)
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "kb",
                "defaults": "standalone",
                "state_path": str(state),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [{"name": "uploads", "type": "document_folder", "path": str(root)}],
        }
    )
    return SyncEngine(config)


def _upload_paths(engine: SyncEngine) -> set[str]:
    return {
        str(dict(row)["relative_path"])
        for row in engine.state.rows(
            "SELECT relative_path FROM artifacts WHERE source_id='uploads'"
        )
    }


def test_removing_every_document_of_an_uploaded_archive_removes_the_archive(tmp_path: Path) -> None:
    """Removing what an archive gave used to delete nothing: each member's row
    held it out "while the archive is there", so the archive stayed on the share
    for good. Expanding nested archives would then have indexed the inner
    members of an archive the person believed they had removed."""

    from pheasant.services import ServiceContext
    from pheasant.services import archive as service

    engine = _upload_engine(tmp_path)
    try:
        archive_file = engine.config.sources[0].path / "Remarkable_and_misc.zip"
        _upload(tmp_path).replace(archive_file)
        engine.sync_source("uploads")
        indexed = {p for p in _upload_paths(engine) if p.startswith("Remarkable_and_misc.zip/")}
        assert "Remarkable_and_misc.zip/files/notebooks.zip/Notebook one.md" in indexed

        context = ServiceContext(
            config=engine.config, state=engine.state, searcher=None, engine=engine, indexing=None
        )
        service.archive_documents(
            context,
            service.ArchiveRequest(source_name="uploads", folders=["Remarkable_and_misc.zip"]),
        )
        assert not archive_file.exists()
        assert archive_rows.for_source(engine.state, "uploads") == []
        assert not (_upload_paths(engine) & indexed)

        # The same name can be uploaded again and is read in full.
        _upload(tmp_path / "again").replace(archive_file)
        engine.sync_source("uploads")
        assert indexed <= _upload_paths(engine)
    finally:
        engine.close()


def test_an_archive_left_behind_by_an_earlier_release_is_removed_by_the_next_sync(
    tmp_path: Path,
) -> None:
    """The state the Azure region is in: the archive's indexed members were
    removed under a release that kept the file, and its nested members were
    never indexed, so no row names them."""

    engine = _upload_engine(tmp_path)
    try:
        archive_file = engine.config.sources[0].path / "Remarkable_and_misc.zip"
        _upload(tmp_path).replace(archive_file)
        for relative in (
            "Remarkable_and_misc.zip/README.md",
            "Remarkable_and_misc.zip/files/notes.md",
        ):
            archive_rows.add(
                engine.state,
                source_id="uploads",
                relative_path=relative,
                artifact_id=f"file:uploads:{relative}:branch=none",
                kind="removed",
                now="2026-11-16T18:46:05Z",
            )
        engine.sync_source("uploads")
        assert not any(p.startswith("Remarkable_and_misc.zip/") for p in _upload_paths(engine))
        assert not archive_file.exists()
        assert archive_rows.for_source(engine.state, "uploads") == []
    finally:
        engine.close()


def test_removing_part_of_an_archive_keeps_the_rest(tmp_path: Path) -> None:
    from pheasant.services import ServiceContext
    from pheasant.services import archive as service

    engine = _upload_engine(tmp_path)
    try:
        archive_file = engine.config.sources[0].path / "Remarkable_and_misc.zip"
        _upload(tmp_path).replace(archive_file)
        engine.sync_source("uploads")
        context = ServiceContext(
            config=engine.config, state=engine.state, searcher=None, engine=engine, indexing=None
        )
        service.archive_documents(
            context, service.ArchiveRequest(documents=["uploads/Remarkable_and_misc.zip/README.md"])
        )
        engine.sync_source("uploads")
        assert archive_file.exists()
        paths = _upload_paths(engine)
        assert "Remarkable_and_misc.zip/README.md" not in paths
        assert "Remarkable_and_misc.zip/files/notes.md" in paths
        assert [r["relative_path"] for r in archive_rows.for_source(engine.state, "uploads")] == [
            "Remarkable_and_misc.zip/README.md"
        ]
    finally:
        engine.close()
