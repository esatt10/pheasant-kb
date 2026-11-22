"""Exporting a whole source as one file (`services.source_export`).

Four things are held here:

1. **Everything comes back.** Every document of the source, once each, with
   the text `read_document` gives for it, in all three formats; a zip carries
   a binary document's own bytes while they are the ones indexed.
2. **Paging by size loses nothing.** Following ``next_offset`` from the first
   part to the last visits every document exactly once, and a part always
   makes progress, even past the size budget.
3. **Both surfaces, one implementation**, refusal text included; HTTP's
   ``download=true`` sends the same bytes the JSON answer encodes.
4. **The listing's scope.** An archived document is not exported, for the
   same reason it is not listed.
"""

from __future__ import annotations

import base64
import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.mcp_server.tools import PheasantTools
from pheasant.services import source_export
from pheasant.services.errors import InvalidRequest, ServiceError, SourceNotFound

pytestmark = pytest.mark.module_database

#: A 1x1 PNG: a document whose original bytes a zip should carry as-is.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

CORPUS: dict[str, str | bytes] = {
    "README.md": "# Readme\n\nWelcome to the reference.\n",
    "records/customer.md": "# Customer\n\n## Fields\n\n- entityid\n- companyname\n",
    "records/salesorder.md": "# Sales order\n\nTransaction record. Café — naïve UTF-8 text.\n",
    "schema/workorder.md": "\n".join(f"Line {n:03d} of the work order schema." for n in range(80))
    + "\n",
    "img/logo.png": PNG,
}


@pytest.fixture(scope="module")
def region(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("export")
    workspace = root / "workspace"
    for relative, body in CORPUS.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "exporter",
                "defaults": "standalone",
                "state_path": str(root / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False},
            "assistant": {"provider": "none"},
            "sources": [
                {
                    "name": "reference",
                    "type": "document_folder",
                    "path": str(workspace),
                    "include": ["**/*"],
                    "chunking": {"max_chars": 400, "overlap_chars": 120},
                }
            ],
        }
    )
    tools = PheasantTools(config)
    tools.engine.sync_source("reference", "full")
    tools.engine.reload_graph()
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))
    return {"tools": tools, "client": client, "kb": config.knowledge_base_id}


def _decode(part: dict[str, Any]) -> bytes:
    if part["encoding"] == "utf-8":
        return part["content"].encode("utf-8")
    return base64.b64decode(part["content"])


def _listed(region: dict[str, Any]) -> list[str]:
    listing = region["tools"].list_documents(region["kb"], "reference", limit=500)
    return sorted(d["path"] for d in listing["documents"])


def test_jsonl_holds_every_document_with_the_text_read_document_gives(
    region: dict[str, Any],
) -> None:
    part = region["tools"].export_source(region["kb"], "reference", "jsonl")

    assert part["encoding"] == "utf-8"
    assert part["filename"] == "reference.jsonl"
    assert (part["has_more"], part["next_offset"]) == (False, None)
    rows = [json.loads(line) for line in part["content"].splitlines()]
    assert [r["path"] for r in rows] == _listed(region)
    assert part["documents"] == part["total"] == len(rows)
    for row in rows:
        read = region["tools"].read_document(region["kb"], row["path"], "reference")
        assert row["content"] == read["content"], row["path"]
        assert row["outline"] == read["outline"], row["path"]
        assert row["content_origin"] == read["content_origin"], row["path"]
        assert row["source"] == "reference"
        assert {"id", "sha256", "viewer", "content_origin", "outline"} <= set(row)
    by_path = {r["path"]: r for r in rows}
    assert by_path["records/salesorder.md"]["content"] == CORPUS["records/salesorder.md"]
    assert "chunks" not in rows[0]


def test_zip_carries_files_at_their_paths_originals_included(region: dict[str, Any]) -> None:
    part = region["tools"].export_source(region["kb"], "reference", "zip", include_chunks=True)

    assert part["encoding"] == "base64"
    archive = zipfile.ZipFile(io.BytesIO(_decode(part)))
    names = set(archive.namelist())
    assert {"README.md", "manifest.jsonl", "chunks.jsonl"} <= names
    for relative, body in CORPUS.items():
        member = f"files/{relative}"
        assert member in names, member
        expected = body if isinstance(body, bytes) else body.encode("utf-8")
        assert archive.read(member) == expected, member
    manifest = [json.loads(line) for line in archive.read("manifest.jsonl").splitlines()]
    assert sorted(m["path"] for m in manifest) == _listed(region)
    assert all("content" not in m and m["file"] for m in manifest)
    chunks = [json.loads(line) for line in archive.read("chunks.jsonl").splitlines()]
    ids = {m["id"] for m in manifest}
    assert chunks and {c["document_id"] for c in chunks} <= ids
    # The long file was cut into overlapping chunks: all of them are there.
    long_id = next(m["id"] for m in manifest if m["path"] == "schema/workorder.md")
    assert len([c for c in chunks if c["document_id"] == long_id]) > 1


def test_parquet_reads_back_as_the_jsonl_rows(region: dict[str, Any], tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    part = region["tools"].export_source(region["kb"], "reference", "parquet")
    jsonl = region["tools"].export_source(region["kb"], "reference", "jsonl")

    target = tmp_path / part["filename"]
    target.write_bytes(_decode(part))
    table = (
        duckdb.connect()
        .execute(
            "SELECT path, content, outline, chunks FROM read_parquet(?) ORDER BY path",
            [str(target)],
        )
        .fetchall()
    )
    rows = sorted(
        (json.loads(line) for line in jsonl["content"].splitlines()), key=lambda r: r["path"]
    )
    assert [t[0] for t in table] == [r["path"] for r in rows]
    for (path, content, outline, chunks), row in zip(table, rows, strict=True):
        assert content == row["content"], path
        assert json.loads(outline) == row["outline"]
        assert chunks is None


def test_following_next_offset_visits_every_document_once(
    region: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A budget smaller than any document: one document per part, which is the
    # hardest case for an off-by-one, and proves a part always makes progress.
    monkeypatch.setattr(source_export, "MAX_PART_BYTES", 1)
    seen: list[str] = []
    offset: int | None = 0
    parts = 0
    while offset is not None:
        part = region["tools"].export_source(region["kb"], "reference", "jsonl", offset)
        assert part["documents"] == 1
        assert part["filename"] == f"reference-{offset:06d}.jsonl"
        seen += [json.loads(line)["path"] for line in part["content"].splitlines()]
        offset = part["next_offset"]
        parts += 1
    assert seen == _listed(region)
    assert parts == len(seen)


def test_http_answers_as_mcp_does_and_downloads_the_same_bytes(region: dict[str, Any]) -> None:
    mcp = region["tools"].export_source(region["kb"], "reference", "jsonl")
    response = region["client"].get("/sources/reference/export", params={"format": "jsonl"})
    assert response.status_code == 200
    assert response.json() == mcp

    zipped = region["tools"].export_source(region["kb"], "reference", "zip")
    download = region["client"].get(
        "/sources/reference/export", params={"format": "zip", "download": "true"}
    )
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    assert 'filename="reference.zip"' in download.headers["content-disposition"]
    assert download.headers["x-pheasant-has-more"] == "false"
    assert "x-pheasant-next-offset" not in download.headers
    # Deflate is deterministic over the same members in the same order.
    assert download.content == _decode(zipped)


@pytest.mark.parametrize(
    ("params", "args", "kwargs", "status", "refusal"),
    [
        ({"format": "csv"}, ("reference", "csv"), {}, 422, InvalidRequest),
        ({"offset": 999}, ("reference", "zip", 999), {}, 422, InvalidRequest),
    ],
)
def test_refusals_read_the_same_on_both_surfaces(  # noqa: PLR0913 - one per column
    region: dict[str, Any],
    params: dict[str, Any],
    args: tuple,
    kwargs: dict[str, Any],
    status: int,
    refusal: type[ServiceError],
) -> None:
    response = region["client"].get("/sources/reference/export", params=params)
    with pytest.raises(refusal) as refused:
        region["tools"].export_source(region["kb"], *args, **kwargs)
    assert response.status_code == status
    assert response.json()["detail"] == str(refused.value)


def test_an_unknown_source_is_refused_on_both_surfaces(region: dict[str, Any]) -> None:
    response = region["client"].get("/sources/nowhere/export")
    with pytest.raises(SourceNotFound) as refused:
        region["tools"].export_source(region["kb"], "nowhere")
    assert response.status_code == 404
    assert response.json()["detail"] == str(refused.value)


def test_an_archived_document_is_not_exported(region: dict[str, Any]) -> None:
    tools = region["tools"]
    tools.archive_documents(region["kb"], ["records/customer.md"], source_name="reference")
    try:
        part = tools.export_source(region["kb"], "reference", "jsonl")
        paths = [json.loads(line)["path"] for line in part["content"].splitlines()]
        assert "records/customer.md" not in paths
        assert paths == _listed(region)
    finally:
        tools.reinstate_documents(region["kb"], ["records/customer.md"], source_name="reference")


def test_a_folder_exports_alone_on_both_surfaces(region: dict[str, Any]) -> None:
    """The taxonomy view exports where the reader is: one folder, or one file.
    Inside a folder it used to offer nothing, so a reader in an uploaded
    archive's folder could only export the whole source, in parts."""

    tools = region["tools"]
    # The archive test above reinstates records/customer.md with a sync that can
    # still be running; comparing two surfaces across it compares two corpora.
    deadline = time.monotonic() + 60
    while "records/customer.md" not in _listed(region) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert "records/customer.md" in _listed(region)
    part = tools.export_source(region["kb"], "reference", "jsonl", path_prefix="records")
    paths = [json.loads(line)["path"] for line in part["content"].splitlines()]
    expected = [path for path in _listed(region) if path.startswith("records/")]
    assert expected and paths == expected
    assert part["total"] == len(expected) and part["filename"] == "reference-records.jsonl"

    # A trailing slash names the same folder; a file names itself.
    again = tools.export_source(region["kb"], "reference", "jsonl", path_prefix="/records/")
    assert again["content"] == part["content"]
    one = tools.export_source(
        region["kb"], "reference", "jsonl", path_prefix="records/salesorder.md"
    )
    assert [json.loads(line)["path"] for line in one["content"].splitlines()] == [
        "records/salesorder.md"
    ]
    # A folder is a path segment: "rec" is not a prefix of "records/...".
    with pytest.raises(InvalidRequest, match="No document under 'rec'"):
        tools.export_source(region["kb"], "reference", "jsonl", path_prefix="rec")

    response = region["client"].get(
        "/sources/reference/export", params={"format": "jsonl", "path_prefix": "records"}
    )
    assert response.status_code == 200 and response.json() == part
    download = region["client"].get(
        "/sources/reference/export",
        params={"format": "zip", "path_prefix": "records", "download": "true"},
    )
    assert 'filename="reference-records.zip"' in download.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(download.content)).namelist()
    assert {f"files/{path}" for path in expected} <= set(names)
    assert not any(name.startswith("files/schema/") for name in names)

    refused = region["client"].get(
        "/sources/reference/export", params={"format": "jsonl", "path_prefix": "rec"}
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == "No document under 'rec' in 'reference'"


def test_a_download_is_one_archive_of_the_whole_tree(
    region: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exporting an uploads source of 48 documents came back as seven zips, each
    holding a slice of the tree, because the download used the JSON answer's
    32 MB pages. A download is one file: here MCP would need a part per
    document, and the download is still one archive with the source's tree."""

    monkeypatch.setattr(source_export, "MAX_PART_BYTES", 1)
    paged = region["tools"].export_source(region["kb"], "reference", "zip")
    assert paged["has_more"] and paged["documents"] == 1

    monkeypatch.setattr(source_export.tempfile, "tempdir", str(tmp_path))
    download = region["client"].get(
        "/sources/reference/export", params={"format": "zip", "download": "true"}
    )
    assert download.status_code == 200
    assert 'filename="reference.zip"' in download.headers["content-disposition"]
    assert download.headers["x-pheasant-has-more"] == "false"
    assert "x-pheasant-next-offset" not in download.headers
    names = set(zipfile.ZipFile(io.BytesIO(download.content)).namelist())
    assert {f"files/{path}" for path in _listed(region) if not path.endswith(".md")} <= names
    assert {
        "files/README.md",
        "files/records/salesorder.md",
        "manifest.jsonl",
        "README.md",
    } <= names
    manifest = zipfile.ZipFile(io.BytesIO(download.content)).read("manifest.jsonl").splitlines()
    assert len(manifest) == int(download.headers["x-pheasant-total"]) == len(_listed(region))
    # The file it was written to is gone once it has been sent.
    assert not list(tmp_path.glob("pheasant-export-*"))


def test_a_download_past_the_file_ceiling_continues_in_a_second(
    region: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_export, "MAX_FILE_BYTES", 1)
    first = region["client"].get(
        "/sources/reference/export", params={"format": "jsonl", "download": "true"}
    )
    assert first.headers["x-pheasant-documents"] == "1"
    assert first.headers["x-pheasant-has-more"] == "true"
    assert first.headers["x-pheasant-next-offset"] == "1"
    assert 'filename="reference-000000.jsonl"' in first.headers["content-disposition"]
