"""Tar archives, plain and compressed, read like ZIPs.

Acceptance:

1. ``.tar``, ``.tar.gz``/``.tgz``, ``.tar.bz2``/``.tbz2`` and ``.tar.xz``/``.txz``
   list their supported members and read each one, never extracting to disk.
2. The ZIP's safety rules hold: no absolute or ``..`` path, no symbolic or hard
   link, no device, and a member is read within its declared size.
3. Archives nest across formats (a ZIP in a ``.tgz``, a ``.tgz`` in a ZIP) and
   read through the chain.
4. A compressed tar is decompressed once per process, not once per member,
   and never past ``MAX_TAR_INFLATED_BYTES``; a changed file is read afresh.
5. A folder source and an upload index a tar's members, a damaged one is named
   unreadable, its members leave with it when removed, and an Open Knowledge
   Format bundle inside a ``.tgz`` is detected and annotated.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.sync import zip_archive
from pheasant.sync.archived import archive_of
from pheasant.sync.zip_archive import ArchiveError, read_member, scan

FILES = {
    "handbook/guides/overview.md": b"# Overview\n\nThe cedar relay is ready.\n",
    "handbook/notes.txt": b"Plain notes.\n",
    "handbook/tool.bin": b"\x00\x01",
}
MODES = {
    ".tar": "w",
    ".tar.gz": "w:gz",
    ".tgz": "w:gz",
    ".tar.bz2": "w:bz2",
    ".tbz2": "w:bz2",
    ".tar.xz": "w:xz",
    ".txz": "w:xz",
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    zip_archive.close_cached()
    yield
    zip_archive.close_cached()


def _add(archive: tarfile.TarFile, name: str, data: bytes, **kind: Any) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for key, value in kind.items():
        setattr(info, key, value)
    archive.addfile(info, io.BytesIO(data) if info.isreg() else None)


def _tar_bytes(files: dict[str, bytes], mode: str = "w:gz") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, data in files.items():
            _add(archive, name, data)
    return buffer.getvalue()


@pytest.mark.parametrize("suffix", sorted(MODES))
def test_every_tar_format_lists_and_reads_its_members(tmp_path: Path, suffix: str):
    path = tmp_path / f"handbook{suffix}"
    path.write_bytes(_tar_bytes(FILES, MODES[suffix]))
    listing = scan(path)
    names = [name for name, _ in listing.members]
    assert names == ["handbook/guides/overview.md", "handbook/notes.txt"]
    assert listing.skipped[".bin"] == 1
    entry = dict(listing.members)["handbook/guides/overview.md"].filename
    assert read_member(path, entry, 1024) == FILES["handbook/guides/overview.md"]


def test_unsafe_tar_entries_are_never_listed(tmp_path: Path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        _add(archive, "ok/a.md", b"# A\n")
        _add(archive, "/etc/passwd.md", b"root")
        _add(archive, "../escape.md", b"x")
        _add(archive, "ok/../../escape2.md", b"x")
        _add(archive, "ok/link.md", b"", type=tarfile.SYMTYPE, linkname="/etc/shadow")
        _add(archive, "ok/hard.md", b"", type=tarfile.LNKTYPE, linkname="ok/a.md")
        _add(archive, "ok/dev.md", b"", type=tarfile.CHRTYPE)
        _add(archive, "ok/fifo.md", b"", type=tarfile.FIFOTYPE)
    path = tmp_path / "unsafe.tar"
    path.write_bytes(buffer.getvalue())
    assert [name for name, _ in scan(path).members] == ["ok/a.md"]
    for entry in ("/etc/passwd.md", "../escape.md", "ok/link.md", "ok/hard.md"):
        with pytest.raises(ArchiveError):
            read_member(path, entry, 1024)


def test_a_member_is_read_within_the_file_limit(tmp_path: Path):
    path = tmp_path / "big.tgz"
    path.write_bytes(_tar_bytes({"a.md": b"x" * 5000}))
    with pytest.raises(ArchiveError, match="file limit"):
        read_member(path, "a.md", 100)


def test_archives_nest_across_formats(tmp_path: Path):
    inner_zip = io.BytesIO()
    with zipfile.ZipFile(inner_zip, "w") as archive:
        archive.writestr("deep/c.md", "# C\n\nInside a ZIP inside a tgz.\n")
    tgz = tmp_path / "outer.tgz"
    tgz.write_bytes(_tar_bytes({"x/inner.zip": inner_zip.getvalue(), "x/b.md": b"# B\n"}))
    listing = scan(tgz)
    assert "x/inner.zip/deep/c.md" in dict(listing.members) and listing.nested == 1
    entry = dict(listing.members)["x/inner.zip/deep/c.md"].filename
    assert entry == "x/inner.zip//deep/c.md"
    assert b"Inside a ZIP" in read_member(tgz, entry, 1024)

    outer_zip = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer_zip, "w") as archive:
        archive.writestr("pack/inner.tar.gz", _tar_bytes({"d.md": b"# D\n\nIn a tar in a ZIP.\n"}))
    listing = scan(outer_zip)
    entry = dict(listing.members)["pack/inner.tar.gz/d.md"].filename
    assert b"In a tar in a ZIP" in read_member(outer_zip, entry, 1024)


def test_a_compressed_tar_is_decompressed_once(tmp_path: Path, monkeypatch):
    path = tmp_path / "many.tgz"
    files = {f"n/{index:03d}.md": f"# {index}\n".encode() for index in range(40)}
    path.write_bytes(_tar_bytes(files))
    opened: list[str] = []
    real = tarfile.open

    def counting(name=None, *args, **kwargs):
        if name is not None and str(name) == str(path):
            opened.append(str(name))
        return real(name, *args, **kwargs)

    monkeypatch.setattr(zip_archive.tarfile, "open", counting)
    listing = scan(path)
    for _name, info in listing.members:
        read_member(path, info.filename, 1024)
    assert len(opened) == 1, "one decompression for the listing and every read"

    path.write_bytes(_tar_bytes({"n/000.md": b"# changed\n"}))
    assert read_member(path, "n/000.md", 1024) == b"# changed\n", "a changed file is read afresh"


def test_inflation_is_bounded(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(zip_archive, "MAX_TAR_INFLATED_BYTES", 4096)
    path = tmp_path / "bomb.tar.gz"
    path.write_bytes(_tar_bytes({"a.md": b"0" * 200_000}))
    with pytest.raises(ArchiveError, match="inflates past"):
        scan(path)


def test_a_damaged_tar_is_an_archive_error(tmp_path: Path):
    path = tmp_path / "broken.tgz"
    path.write_bytes(b"not a gzip stream at all")
    with pytest.raises(ArchiveError):
        scan(path)


def test_removal_and_registry_know_tar_names():
    assert archive_of("handbook.tgz/handbook/a.md") == "handbook.tgz"
    assert archive_of("x/handbook.tar.gz/a.md") == "x/handbook.tar.gz"
    assert archive_of("notes.gz/a.md") is None, "a single gzipped file is not an archive"
    assert zip_archive.archive_mime("a.tar.xz") == "application/x-xz"


# ------------------------------------------------------------- through a source


def test_a_folder_source_indexes_a_tar_and_names_a_damaged_one(tmp_path: Path):
    from pheasant.config.schema import PheasantConfig
    from pheasant.sync.engine import SyncEngine

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "handbook.TGZ").write_bytes(_tar_bytes(FILES))
    (corpus / "broken.tar.xz").write_bytes(b"damaged")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "tars",
                "defaults": "standalone",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": [{"name": "kb", "type": "document_folder", "path": str(corpus)}],
        }
    )
    engine = SyncEngine(config)
    try:
        result = engine.sync_source("kb", "full")
        paths = {
            row["relative_path"]
            for row in engine.state.rows("SELECT relative_path FROM artifacts", ())
        }
        assert "handbook.TGZ/handbook/guides/overview.md" in paths
        assert "handbook.TGZ/handbook/notes.txt" in paths
        unreadable = str(result.details)
        assert "broken.tar.xz" in unreadable, "a damaged archive is named, not dropped"
    finally:
        engine.close()


def test_an_okf_bundle_uploaded_as_a_tgz_is_detected(
    loaded_config, config_path: Path, tmp_path: Path
):
    from pheasant.api.app import create_app
    from tests.test_okf_bundles import write_bundle

    corpus = write_bundle(tmp_path / "acme")
    files = {
        f"acme/{path.relative_to(corpus).as_posix()}": path.read_bytes()
        for path in sorted(corpus.rglob("*"))
        if path.is_file()
    }
    app = create_app(config=loaded_config, config_path=config_path)
    with TestClient(app) as client:
        response = client.post(
            "/sources/upload",
            files=[("files", ("acme.tgz", io.BytesIO(_tar_bytes(files)), "application/gzip"))],
            data={"source_name": "tar-uploads", "sync_now": "true", "wait": "true"},
        )
        assert response.status_code == 200, response.text
        source = next(s for s in client.get("/sources").json() if s["name"] == "tar-uploads")
        assert source["uploaded_archives"] == ["acme.tgz"]
        graph = app.state.engine.graph_builder.graph
        with graph.reading():
            bundles = [
                attrs.get("okf_root")
                for _node, attrs in graph.nodes(data=True)
                if attrs.get("type") == "okf_bundle"
            ]
        assert bundles == ["acme.tgz/acme"]
        hits = client.post("/search", json={"query": "Gross Margin legacy", "mode": "text"}).json()
        legacy = next(
            h
            for h in hits["results"]
            if h.get("relative_path") == "acme.tgz/acme/metrics/margin-legacy.md"
        )
        assert legacy["okf"]["index"] == "acme.tgz/acme/metrics/index.md"
    app.state.engine.close()
