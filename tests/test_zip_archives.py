"""Filesystem ZIP discovery, filtering and resource boundaries."""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.ingestion.captioner import source_includes_images
from pheasant.ingestion.extractor import source_includes_documents
from pheasant.ingestion.transcriber import source_includes_audio
from pheasant.ingestion.walk import SyncBudgetExceeded
from pheasant.persistence.state_store import StateStore
from pheasant.sync.connectors import FilesystemConnector
from pheasant.sync.zip_archive import ArchiveError, members, read_member


def _connector(tmp_path: Path, **source_overrides: object) -> FilesystemConnector:
    config = PheasantConfig.model_validate(
        {
            "sources": [
                {
                    "name": "archive-test",
                    "type": "document_folder",
                    "path": str(tmp_path),
                    "include": ["**/*.zip"],
                    **source_overrides,
                }
            ]
        }
    )
    state = StateStore(tmp_path / "state.db")
    return FilesystemConnector(config.effective_source(config.sources[0]), state)


def test_zip_members_respect_filters_and_unsafe_paths(tmp_path: Path) -> None:
    archive_path = tmp_path / "mixed.ZIP"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("nested/a.pdf", b"pdf")
        archive.writestr("nested/private/secret.pdf", b"secret")
        archive.writestr("nested/b.md", b"markdown")
        archive.writestr("./nested/relative.md", b"relative")
        archive.writestr("nested\\windows.md", b"windows")
        archive.writestr("nested/c.zip", b"nested archive")
        archive.writestr("../escape.pdf", b"unsafe")
        archive.writestr("/absolute.pdf", b"unsafe")
        link = zipfile.ZipInfo("nested/link.pdf")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, b"target")

    connector = _connector(
        tmp_path,
        include=["nested/**/*.pdf", "nested/a.pdf"],
        exclude=["nested/private/*"],
    )
    items = connector.list_items()
    assert [item.relative_path for item in items] == ["mixed.ZIP/nested/a.pdf"]
    assert connector.read_item(items[0]).content == b"pdf"

    all_items = _connector(tmp_path).list_items()
    assert {item.relative_path for item in all_items} == {
        "mixed.ZIP/nested/a.pdf",
        "mixed.ZIP/nested/private/secret.pdf",
        "mixed.ZIP/nested/b.md",
        "mixed.ZIP/nested/relative.md",
        "mixed.ZIP/nested/windows.md",
    }
    assert {connector.read_item(item).content for item in all_items} == {
        b"pdf",
        b"secret",
        b"markdown",
        b"relative",
        b"windows",
    }


def test_zip_member_size_and_total_budget_are_enforced(tmp_path: Path) -> None:
    archive_path = tmp_path / "large.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.txt", b"a" * 700_000)
        archive.writestr("b.txt", b"b" * 700_000)
        archive.writestr("oversized.txt", b"c" * 1_100_000)

    connector = _connector(
        tmp_path,
        limits={"max_file_size_mb": 1, "max_total_mb": 4},
    )
    items = connector.list_items()
    assert [item.relative_path for item in items] == [
        "large.zip/a.txt",
        "large.zip/b.txt",
    ]
    with pytest.raises(ArchiveError):
        read_member(archive_path, "oversized.txt", 1_000_000)

    bounded = _connector(
        tmp_path,
        limits={"max_file_size_mb": 1, "max_total_mb": 1},
    )
    with pytest.raises(SyncBudgetExceeded, match="more than 1 MB"):
        bounded.list_items()


def test_duplicate_zip_member_names_are_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "duplicate.zip"
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("same.md", b"one")
            archive.writestr("same.md", b"two")
    with pytest.raises(ArchiveError, match="duplicate member"):
        members(archive_path)

    alias_path = tmp_path / "aliases.zip"
    with zipfile.ZipFile(alias_path, "w") as archive:
        archive.writestr("same.md", b"one")
        archive.writestr("./same.md", b"two")
    with pytest.raises(ArchiveError, match="duplicate member"):
        members(alias_path)


def test_zip_include_enables_all_content_handlers(tmp_path: Path) -> None:
    source = _connector(tmp_path).source
    assert source_includes_documents(source)
    assert source_includes_images(source)
    assert source_includes_audio(source)
