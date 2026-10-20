"""Removing one document from the index, and keeping it removed.

The properties: an archived document leaves every store (state rows, full-text
rows, vectors, graph nodes), stays out through incremental *and* full syncs,
is never read while archived, and comes back with everything rebuilt when
reinstated. An upload is deleted outright and cannot be reinstated. A fleet's
api replica only writes the row and publishes a sync; the indexer's sync does
the removal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.persistence import archive as archive_rows
from pheasant.services import ServiceContext
from pheasant.services import archive as service
from pheasant.services.errors import AmbiguousDocument, DocumentNotFound, NotReinstatable
from pheasant.sync.engine import SyncEngine
from tests.conftest import make_vector_engine


def _engine(tmp_path: Path, *, upload: bool = False) -> SyncEngine:
    state = tmp_path / "state"
    if upload:
        root = state / "uploads" / "uploads"
        source = {"name": "uploads", "type": "document_folder", "path": str(root)}
    else:
        root = tmp_path / "corpus"
        source = {"name": "notes", "type": "markdown_folder", "path": str(root)}
    (root / "guides").mkdir(parents=True)
    (root / "other").mkdir(parents=True)
    (root / "guides" / "rotation.md").write_text(
        "# Rotation\n\nRotate the gateway credentials quarterly.\n", encoding="utf-8"
    )
    (root / "guides" / "deploy.md").write_text(
        "# Deploy\n\nDeploy with the release script.\n", encoding="utf-8"
    )
    (root / "other" / "deploy.md").write_text("# Other\n\nA second deploy.\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "archive",
                "state_path": str(state),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [source],
        }
    )
    engine = SyncEngine(config)
    engine.sync_source(source["name"], "full")
    return engine


def _context(engine: SyncEngine, indexing: Any = None) -> ServiceContext:
    return ServiceContext(
        config=engine.config, state=engine.state, searcher=None, engine=engine, indexing=indexing
    )


def _paths(engine: SyncEngine, source: str) -> set[str]:
    return {
        str(row["relative_path"])
        for row in engine.state.rows(
            "SELECT relative_path FROM artifacts WHERE source_id=?", (source,)
        )
    }


def _held_text(engine: SyncEngine, source: str, path: str) -> dict[str, int]:
    """Where a document's content can still be found, store by store."""

    artifact = f"file:{source}:{path}:"
    graph = engine.graph_builder.graph
    return {
        "chunks": len(
            engine.state.rows(
                "SELECT 1 FROM chunks WHERE source_id=? AND artifact_id LIKE ?",
                (source, f"{artifact}%"),
            )
        ),
        "fts": len(engine.state.rows("SELECT 1 FROM chunks_fts WHERE text LIKE ?", ("%gateway%",))),
        "graph": sum(1 for node_id in graph.nodes if str(node_id).startswith(artifact)),
        # What a serving replica reads: the committed rows, not the working set.
        "graph_rows": len(
            engine.state.rows(
                "SELECT 1 FROM graph_nodes WHERE source_id=? AND relative_path=?", (source, path)
            )
        ),
    }


def test_an_archived_document_leaves_the_index_and_stays_out_of_every_sync(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    try:
        context = _context(engine)
        before = _held_text(engine, "notes", "guides/rotation.md")
        assert all(before.values()), before

        result = service.archive_documents(
            context, service.ArchiveRequest(documents=["notes/guides/rotation.md"])
        )

        assert result["status"] == "removed"
        assert result["documents"][0]["disposition"] == "archived"
        assert result["documents"][0]["reinstatable"] is True
        assert _held_text(engine, "notes", "guides/rotation.md") == {
            "chunks": 0,
            "fts": 0,
            "graph": 0,
            "graph_rows": 0,
        }
        assert "guides/rotation.md" not in engine.manifests.load("notes")["artifacts"]
        # The file itself is untouched: an archived document is reinstatable.
        assert (tmp_path / "corpus" / "guides" / "rotation.md").is_file()

        for mode in ("incremental", "full", "incremental"):
            synced = engine.sync_source("notes", mode)
            assert "guides/rotation.md" not in _paths(engine, "notes"), mode
            assert synced.details["archived_documents"] == 1
        assert _paths(engine, "notes") == {"guides/deploy.md", "other/deploy.md"}

        listed = service.archived_documents(context, service.ArchivedRequest(source_name="notes"))
        assert [d["path"] for d in listed["documents"]] == ["guides/rotation.md"]
        assert listed["total"] == 1
    finally:
        engine.close()


def test_an_archived_file_is_never_read(tmp_path: Path, monkeypatch) -> None:
    engine = _engine(tmp_path)
    try:
        service.archive_documents(
            _context(engine), service.ArchiveRequest(documents=["guides/rotation.md"])
        )
        from pheasant.sync import connectors

        read: list[str] = []
        original = connectors.FilesystemConnector.read_item

        def spy(self, item):
            read.append(item.relative_path)
            return original(self, item)

        monkeypatch.setattr(connectors.FilesystemConnector, "read_item", spy)
        engine.sync_source("notes", "full")
        assert read and "guides/rotation.md" not in read
    finally:
        engine.close()


def test_reinstating_rebuilds_the_content_with_an_incremental_sync(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        context = _context(engine)
        before = _held_text(engine, "notes", "guides/rotation.md")
        service.archive_documents(context, service.ArchiveRequest(documents=["rotation.md"]))

        result = service.reinstate_documents(
            context, service.ArchiveRequest(documents=["notes/guides/rotation.md"])
        )

        assert result["status"] == "reinstated"
        assert result["sources"]["notes"]["status"] == "synced"
        assert result["sources"]["notes"]["indexed"] == 1
        assert _held_text(engine, "notes", "guides/rotation.md") == before
        assert archive_rows.for_source(engine.state, "notes") == []
        # An unchanged re-sync after reinstatement does no work (pillar 2).
        assert engine.sync_source("notes", "incremental").indexed_artifacts == 0
    finally:
        engine.close()


def test_archiving_prunes_vectors_and_reinstating_restores_them(tmp_path: Path) -> None:
    engine = make_vector_engine(tmp_path)
    try:
        engine.sync_source("notes", "full")
        store = engine.vectors.store
        baseline = store.count()
        context = _context(engine)
        service.archive_documents(context, service.ArchiveRequest(documents=["vehicles.md"]))
        live = engine.state.rows("SELECT id FROM chunks WHERE source_id='notes'")
        assert store.count() == len(live) < baseline

        service.reinstate_documents(context, service.ArchiveRequest(documents=["vehicles.md"]))
        assert store.count() == baseline
    finally:
        engine.close()


def test_an_upload_is_deleted_and_cannot_be_reinstated(tmp_path: Path) -> None:
    engine = _engine(tmp_path, upload=True)
    try:
        uploaded = tmp_path / "state" / "uploads" / "uploads" / "guides" / "rotation.md"
        assert uploaded.is_file()
        result = service.archive_documents(
            _context(engine), service.ArchiveRequest(documents=["uploads/guides/rotation.md"])
        )

        assert result["documents"][0]["disposition"] == "removed"
        assert result["documents"][0]["reinstatable"] is False
        assert not uploaded.exists()
        # The bytes are gone, so the row has nothing left to hold out.
        assert archive_rows.for_source(engine.state, "uploads") == []
        engine.sync_source("uploads", "full")
        assert "guides/rotation.md" not in _paths(engine, "uploads")
        with pytest.raises(DocumentNotFound):
            service.reinstate_documents(
                _context(engine), service.ArchiveRequest(documents=["guides/rotation.md"])
            )
    finally:
        engine.close()


def test_an_api_replica_writes_the_row_and_the_indexers_sync_removes_it(tmp_path: Path) -> None:
    engine = _engine(tmp_path, upload=True)
    published: list[str] = []

    def publish(source: str) -> dict:
        published.append(source)
        return {"status": "queued", "queued_tasks": [f"idx-{source}"]}

    try:
        replica = _context(engine, service.Indexing(local=False, start_sync=publish))
        result = service.archive_documents(
            replica, service.ArchiveRequest(documents=["guides/rotation.md"])
        )
        assert result["status"] == "queued"
        assert result["sources"]["uploads"]["status"] == "removal_queued"
        assert published == ["uploads"]
        # Nothing removed yet, and the upload cannot be reinstated meanwhile.
        assert "guides/rotation.md" in _paths(engine, "uploads")
        with pytest.raises(NotReinstatable):
            service.reinstate_documents(
                replica, service.ArchiveRequest(documents=["guides/rotation.md"])
            )

        # The indexer claims the published task: an ordinary incremental sync.
        engine.sync_source("uploads", "incremental")
        assert "guides/rotation.md" not in _paths(engine, "uploads")
        assert not (tmp_path / "state" / "uploads" / "uploads" / "guides" / "rotation.md").exists()
        assert archive_rows.for_source(engine.state, "uploads") == []
    finally:
        engine.close()


def test_a_name_must_name_exactly_one_document(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        context = _context(engine)
        with pytest.raises(AmbiguousDocument) as ambiguous:
            service.archive_documents(context, service.ArchiveRequest(documents=["deploy.md"]))
        assert "notes/guides/deploy.md" in str(ambiguous.value)
        with pytest.raises(DocumentNotFound):
            service.archive_documents(context, service.ArchiveRequest(documents=["missing.md"]))
        with pytest.raises(DocumentNotFound):
            service.reinstate_documents(context, service.ArchiveRequest(documents=["deploy.md"]))
        # Nothing was archived by the refusals.
        assert archive_rows.for_source(engine.state, "notes") == []
        assert len(_paths(engine, "notes")) == 3
    finally:
        engine.close()


def test_both_surfaces_archive_list_and_refuse_identically(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app
    from pheasant.mcp_server.tools import PheasantTools

    engine = _engine(tmp_path)
    config = engine.config
    engine.close()
    tools = PheasantTools(config)
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))
    kb = config.knowledge_base_id
    try:
        # One refusal, one text, on both surfaces.
        for route, body, call, status in (
            ("/documents/archive", ["deploy.md"], tools.archive_documents, 409),
            ("/documents/archive", ["missing.md"], tools.archive_documents, 404),
            ("/documents/reinstate", ["rotation.md"], tools.reinstate_documents, 404),
            ("/documents/archive", [], tools.archive_documents, 422),
        ):
            response = client.post(route, json={"documents": body})
            with pytest.raises(ValueError) as refused:
                call(kb, body)
            assert response.status_code == status, response.text
            assert response.json()["detail"] == str(refused.value)

        over_mcp = tools.archive_documents(kb, ["notes/guides/rotation.md"])
        over_http = client.post("/documents/archive", json={"documents": ["other/deploy.md"]})
        assert over_mcp["status"] == over_http.json()["status"] == "removed"
        listed = client.get("/documents/archived", params={"source_name": "notes"}).json()
        assert listed == tools.list_archived_documents(kb, "notes")
        assert [d["path"] for d in listed["documents"]] == ["guides/rotation.md", "other/deploy.md"]
        assert {d["archived_by"] for d in listed["documents"]} == {"mcp", "http"}
    finally:
        client.close()
        tools.engine.close()


def test_at_remove_in_the_chat_archives_what_it_names(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from pheasant.api.app import create_app

    engine = _engine(tmp_path)
    config = engine.config
    config.assistant.provider = "none"
    engine.close()
    client = TestClient(create_app(config, config_path=str(tmp_path / "pheasant.yaml")))

    def ask(question: str, **extra: Any) -> dict:
        response = client.post("/assistant/chat", json={"question": question, **extra})
        assert response.status_code == 200, response.text
        return response.json()

    try:
        removed = ask("@remove notes/guides/rotation.md")
        assert removed["archive"]["status"] == "removed"
        assert removed["route"]["intent"] == "remove"
        assert "`notes/guides/rotation.md`: archived" in removed["answer"]
        assert removed["citations"] == []

        ambiguous = ask("@remove deploy.md")
        assert ambiguous["answer"].startswith("Nothing was removed: deploy.md names 2 documents")
        assert "archive" not in ambiguous

        assert "Name the documents to remove" in ask("@remove")["answer"]
        # Nothing open: `@current @remove` removes nothing rather than guessing.
        assert "Name the documents to remove" in ask("@current @remove")["answer"]
        current = ask("@current @remove", current_document="notes/other/deploy.md")
        assert [d["path"] for d in current["archive"]["documents"]] == ["other/deploy.md"]

        tree = client.get("/documents/archived", params={"source_name": "notes"}).json()
        assert [d["path"] for d in tree["documents"]] == ["guides/rotation.md", "other/deploy.md"]
        assert {d["archived_by"] for d in tree["documents"]} == {"assistant"}
        listed = client.get("/documents", params={"source_name": "notes"}).json()
        assert [d["path"] for d in listed["documents"]] == ["guides/deploy.md"]
    finally:
        client.close()


def test_removing_a_source_forgets_what_it_held_out(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        service.archive_documents(
            _context(engine), service.ArchiveRequest(documents=["guides/rotation.md"])
        )
        assert archive_rows.for_source(engine.state, "notes")
        engine.remove_source("notes")
        assert archive_rows.for_source(engine.state, "notes") == []
    finally:
        engine.close()
