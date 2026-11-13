"""Artifact adoption, evidence identity, and outputs that outlive their jobs."""

from __future__ import annotations

import io
import json
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.artifacts import publications
from pheasant.jobs import JobRegistry
from pheasant.persistence.durable_jobs import DurableJobStore
from pheasant.search.ranking import DEFAULT_RANKING, RankingResolver
from pheasant.services import (
    ServiceContext,
    artifact_lifecycle,
    artifact_records,
    artifacts,
    retrieval_configurations,
)
from pheasant.services import artifact_evidence as evidence
from pheasant.services import artifact_render as render
from pheasant.sync.engine import SyncEngine
from pheasant.tuning import store as tuning_store
from pheasant.tuning.contracts import Experiment, ParameterPoint


@pytest.fixture(params=["sqlite", "postgres"])
def context(loaded_config, request):
    if request.param == "postgres":
        dsn = os.environ.get("PHEASANT_TEST_POSTGRES_DSN")
        if not dsn:
            pytest.skip("set a throwaway PHEASANT_TEST_POSTGRES_DSN for backend parity")
        from tests.test_backend_parity import _reset

        _reset(dsn)
        loaded_config.storage.backend = "postgres"
        loaded_config.storage.dsn_env = "PHEASANT_TEST_POSTGRES_DSN"
    engine = SyncEngine(loaded_config)
    try:
        yield ServiceContext(loaded_config, engine.state, None, engine=engine)
    finally:
        engine.close()


def test_records_use_the_artifact_source_and_are_searchable_synchronously(context):
    result = artifact_records.write(
        context,
        artifact_records.WriteRequest(
            "The zephyr bridge is configured in the harbor subsystem.", scope="org"
        ),
    )
    source = artifact_records.source(context)
    assert source.type.value == "artifact" and source.taxonomy.enabled
    rows = context.state.rows("SELECT type FROM artifacts WHERE source_id=?", (source.name,))
    assert rows and rows[0]["type"] == "artifact_record"
    assert context.state.rows("SELECT id FROM chunks WHERE source_id=?", (source.name,))
    viewed = artifacts.get(context, result["artifact_id"])
    assert viewed["payload"]["answer"].startswith("The zephyr bridge")
    assert any(
        a["id"] == result["artifact_id"] for a in artifacts.list_artifacts(context)["artifacts"]
    )


def test_legacy_source_adoption_preserves_files_ids_and_original_definition(context):
    from pheasant.artifacts.store import ArtifactRecordStore
    from pheasant.config.schema import PheasantConfig
    from pheasant.registry.source_registry import SourceRegistry

    directory = context.config.pheasant.state_path.parent / "old-records"
    stored, _ = ArtifactRecordStore(directory).append("An existing assertion.", scope="org")
    original = stored.path.read_bytes()
    source = PheasantConfig.model_validate(
        {
            "sources": [
                {
                    "name": "old-notes",
                    "type": "memory",
                    "path": str(directory),
                }
            ]
        }
    ).sources[0]
    registry = SourceRegistry(context.config, context.state)
    registry.register_source(source)
    registry.register_source(source)
    assert source.type.value == "artifact"
    assert stored.path.read_bytes() == original
    rows = context.state.rows(
        "SELECT * FROM artifact_source_migrations WHERE source_id='old-notes'"
    )
    assert len(rows) == 1 and json.loads(rows[0]["original_config_json"])["type"] == "memory"


def test_legacy_imports_are_the_same_implementation():
    import pheasant.artifacts.store as canonical
    import pheasant.memory.store as legacy

    assert legacy is canonical
    assert legacy.MemoryStore is canonical.ArtifactRecordStore


def test_http_memory_is_deprecated_and_uses_the_same_artifact_operation(loaded_config):
    app = create_app(loaded_config)
    try:
        client = TestClient(app)
        result = client.post(
            "/artifacts/records", json={"text": "A shared artifact.", "scope": "org"}
        )
        assert result.status_code == 200, result.text
        legacy = client.get("/memory")
        assert legacy.headers["deprecation"] == "true"
        canonical = client.get("/artifacts/records", params={"current_only": False})
        assert legacy.json() == canonical.json()
        assert app.openapi()["paths"]["/memory"]["get"]["deprecated"] is True
        candidates = client.get("/artifacts/candidates")
        legacy_candidates = client.get("/memory/candidates")
        assert candidates.status_code == 200
        assert candidates.json() == legacy_candidates.json()
        assert "</artifacts/candidates>" in legacy_candidates.headers["link"]
        recalled = client.post(
            "/search", json={"query": "shared artifact", "source_types": ["memory"]}
        )
        assert recalled.status_code == 200 and recalled.json()["results"]
        assert any(
            r.get("artifact", {}).get("semantic_type") == "fact" for r in recalled.json()["results"]
        ), recalled.json()
    finally:
        app.state.jobs.close()
        app.state.engine.close()


def test_record_visibility_and_replacement_partition_are_preserved(context):
    first = artifact_records.write(
        context,
        artifact_records.WriteRequest("Private knowledge.", principal="user:ada", sync=False),
    )
    assert artifact_records.list_records(context, "user:bea")["records"] == []
    assert artifact_records.list_records(context, "user:ada")["records"]
    with pytest.raises(ValueError, match="visible"):
        artifact_records.write(
            context,
            artifact_records.WriteRequest(
                "Forged correction.",
                principal="user:bea",
                supersedes=first["artifact_id"],
                sync=False,
            ),
        )


def test_dynamic_numbers_preserve_code_links_and_source_identity():
    payload = evidence.normalize(
        {
            "answer": "First [9], next [4], repeated [9]. `items[9]`\n\n"
            "~~~python\nitems[4]\n~~~\n\n    data[9]\n\n[4](https://example.test)",
            "citations": [
                {"index": 4, "source": "a", "path": "guide.md"},
                {"index": 9, "source": "b", "path": "guide.md"},
            ],
        }
    )
    assert payload["answer"].startswith("First [1], next [2], repeated [1]. `items[9]`")
    assert "items[4]" in payload["answer"] and "    data[9]" in payload["answer"]
    assert "[4](https://example.test)" in payload["answer"]
    assert len({c["evidence_id"] for c in payload["citations"]}) == 2
    assert payload["citations"][0]["target"] == "pheasant://document/b/guide.md"
    assert evidence.normalize(payload) is payload
    missing = evidence.normalize(
        {
            "answer": "Known [9], unresolved [1].",
            "citations": [{"index": 9, "source": "docs", "path": "a.md"}],
        }
    )
    assert missing["answer"] == "Known [1], unresolved [?]."
    assert missing["unresolved_citations"] == ["1"]


def test_rendered_and_exported_evidence_is_resolvable_and_code_is_unchanged():
    artifact = {
        "title": "Test",
        "kind": "report",
        "payload": evidence.normalize(
            {
                "answer": "Claim [7]. `items[7]`",
                "files": [{"name": "data.py", "content": "items[7]"}],
                "citations": [
                    {"index": 7, "source": "docs", "path": "guide.md", "snapshot": "Evidence text."}
                ],
            }
        ),
    }
    citation = artifact["payload"]["citations"][0]
    page = render.overview(artifact)
    assert 'href="#source-1"' in page and f'id="evidence-{citation["evidence_id"]}"' in page
    assert "<code>items[7]</code>" in page and "Evidence text." in page
    markdown = render.markdown_export(artifact)
    assert f"### evidence-{citation['evidence_id']}" in markdown
    with zipfile.ZipFile(io.BytesIO(render.zip_export(artifact))) as archive:
        assert archive.read("data.py") == b"items[7]"
        assert archive.read(f"evidence/{citation['evidence_id']}.md").endswith(b"Evidence text.")


def test_published_content_is_immune_to_age_retention_but_drafts_are_not(context):
    published = artifacts.save(context, artifacts.SaveRequest(markdown="# Keep"))
    draft = artifacts.save(context, artifacts.SaveRequest(markdown="# Draft", published=False))
    context.config.artifacts.retention_days = 1
    context.state.execute("UPDATE saved_artifacts SET created_at=?", ("2020-01-01T00:00:00+00:00",))
    outcome = artifact_lifecycle.run_retention(context)
    assert outcome["archived"] == 1
    assert artifacts.get(context, published["id"])["archived_at"] is None
    assert artifacts.get(context, draft["id"])["archived_at"] is not None


def test_jobs_survive_restart_and_deletion_never_changes_publication(context):
    kept = artifacts.save(context, artifacts.SaveRequest(markdown="# Published output"))
    before = publications.status(context.state, context.config.knowledge_base_id, kept["id"])
    jobs = JobRegistry(state=context.state, kb_id=context.config.knowledge_base_id)
    observer = JobRegistry(state=context.state, kb_id=context.config.knowledge_base_id)
    try:
        job = jobs.create("compose", "Produce output")
        jobs.finish(job.id, result={"artifact_id": kept["id"]})
        assert observer.get(job.id)["result"]["artifact_id"] == kept["id"]
        observer.clear(job.id)
        assert jobs.get(job.id) is None
        assert jobs.list() == []
        assert (
            publications.status(context.state, context.config.knowledge_base_id, kept["id"])
            == before
        )
        assert (
            publications.version(
                context.state, context.config.knowledge_base_id, kept["id"], before["revision_id"]
            )["payload"]["answer"]
            == "# Published output"
        )
    finally:
        observer.close()
        jobs.close()


def test_job_retention_keeps_active_jobs_and_published_outputs(context):
    kb = context.config.knowledge_base_id
    store = DurableJobStore(context.state, kb, retention_days=1)
    old = "2020-01-01T00:00:00Z"
    store.write(
        {"id": "old", "active": False, "finished_at": old, "started_at": old, "sources": []}
    )
    store.write({"id": "active", "active": True, "started_at": old, "sources": []})
    assert store.prune() == 1 and store.get("active") is not None


def test_job_listing_bounds_history_without_deleting_it(context):
    store = DurableJobStore(context.state, context.config.knowledge_base_id)
    for day in range(1, 11):
        store.write(
            {"id": f"job-{day}", "active": False, "finished_at": f"2026-01-{day:02d}T00:00:00Z"}
        )
    store.write({"id": "active", "active": True})
    assert {r["id"] for r in store.list(limit=2)} == {"active", "job-9", "job-10"}
    assert store.get("job-1") and len(store.list()) == 11


def test_named_base_can_be_reenabled_after_reconfiguration(context):
    saved = retrieval_configurations.save_base(context, "original")
    context.config.search.ranking.rrf_k = 180.0
    kb = context.config.knowledge_base_id
    tuning_store.apply_bundle(context.state, kb, saved["bundle_id"], applied_by="test")
    resolver = RankingResolver(base=DEFAULT_RANKING, state=context.state, kb_id=kb, ttl_seconds=0)
    assert resolver.current().rrf_k == saved["parameters"]["rrf_k"]


def test_active_or_wrong_region_pruning_cannot_delete_trials(context):

    # A source with no evidence still records a finished experiment; use a
    # real experiment row to test the parent's status guard transactionally.
    kb = context.config.knowledge_base_id
    experiment = Experiment(
        "guarded",
        kb,
        "snapshot",
        "cohort",
        "holdout",
        "control",
        "space",
        {},
        ParameterPoint.of({"rrf_k": 60.0}),
    )
    tuning_store.open_experiment(context.state, experiment, owner="test")
    result = tuning_store.prune_experiment(context.state, kb, experiment.experiment_id)
    assert result == {"experiments": 0, "trials": 0, "decisions": 0}
    assert tuning_store.experiment_status(context.state, experiment.experiment_id) is not None


def test_evaluation_history_deletion_keeps_its_inspectable_output(context):
    from pheasant.evaluation import store
    from pheasant.services.job_history import delete_evaluation

    store.open_run(
        context.state,
        run_id="published-run",
        kb_id=context.config.knowledge_base_id,
        snapshot_id="test-snapshot",
        started_at="2026-01-01T00:00:00Z",
        mode="offline",
        config_digest="test",
    )
    store.close_run(
        context.state,
        run_id="published-run",
        finished_at="2026-01-01T00:01:00Z",
        status="completed",
        gates_passed=True,
        report={"evidence": "self-contained report"},
    )
    # Simulate an older deployment whose completed report predates publications.
    context.state.execute(
        "DELETE FROM artifact_publications WHERE artifact_id='evaluation:published-run'"
    )
    context.state.execute(
        "DELETE FROM artifact_versions WHERE artifact_id='evaluation:published-run'"
    )
    from pheasant.persistence.artifact_migrations import migrate_run_outputs

    with context.state.conn:
        migrate_run_outputs(context.state)
        migrate_run_outputs(context.state)
    original = artifacts.get(context, "evaluation:published-run")
    assert original["publication_status"] == "published"
    assert original["revision_id"]
    delete_evaluation(context, "published-run")
    assert not context.state.rows("SELECT * FROM evaluation_runs WHERE run_id='published-run'")
    assert artifacts.get(context, "evaluation:published-run") == original
    assert not context.state.rows("SELECT * FROM artifacts WHERE source_id='evaluation'")


def test_draft_migration_is_idempotent_and_deletion_deprecates_only_pointer(context):
    from pheasant.persistence.artifact_migrations import migrate_saved_artifacts

    draft = artifacts.save(
        context, artifacts.SaveRequest(markdown="# Unpublished", published=False)
    )
    kept = artifacts.save(context, artifacts.SaveRequest(markdown="# Published"))
    kb = context.config.knowledge_base_id
    before = publications.status(context.state, kb, kept["id"])
    with context.state.conn:
        migrate_saved_artifacts(context.state)
        migrate_saved_artifacts(context.state)
    assert publications.status(context.state, kb, draft["id"]) is None
    assert publications.status(context.state, kb, kept["id"]) == before
    artifacts.delete(context, kept["id"])
    assert publications.status(context.state, kb, kept["id"])["status"] == "deprecated"
    assert publications.version(context.state, kb, kept["id"], before["revision_id"])


def test_file_preview_and_thread_prose_use_the_same_evidence_mapping():
    from pheasant.services.artifact_trace import renumber, transcript

    turns = [
        {
            "turn": 1,
            "question": "Why?",
            "answer": "Claim [8].\n\n~~~\na[8]\n~~~",
            "citations": [{"index": 8, "source": "docs", "path": "a.md", "revision": "r1"}],
            "files": ["turn-1-note.md"],
        }
    ]
    citations = renumber(turns)
    assert citations[0]["revision"] == "r1"
    assert "Claim [1]" in transcript(turns) and "a[8]" in transcript(turns)
    file = {"name": "turn-1-note.md", "content": "Claim [8]. `a[8]`"}
    artifact = {
        "title": "Thread",
        "payload": {
            "answer": transcript(turns),
            "citations": citations,
            "turns": turns,
            "files": [file],
        },
    }
    page = render.file_document(artifact, file, mode="preview")
    assert 'href="#source-1"' in page and "<code>a[8]</code>" in page


def test_live_taxonomy_and_job_retention_settings_are_honored(context):
    from pheasant.services import artifact_settings

    source = artifact_records.source(context, create=True)
    artifact_settings.update(context, {"taxonomy_enabled": False, "job_retention_days": 0}, None)
    assert source.taxonomy.enabled is False
    store = DurableJobStore(context.state, context.config.knowledge_base_id, retention_days=1)
    store.write({"id": "kept", "active": False, "finished_at": "2020-01-01T00:00:00Z"})
    assert store.prune() == 0 and store.get("kept")
    artifact_settings.update(context, {"taxonomy_enabled": True, "job_retention_days": 1}, None)
    assert source.taxonomy.enabled is True
    assert store.prune() == 1 and store.get("kept") is None


def test_private_drafts_expire_without_exposing_them_to_the_retention_actor(context):
    context.config.security.acl_enforced = True
    draft = artifacts.save(
        context,
        artifacts.SaveRequest(
            markdown="# Private draft",
            published=False,
            scope="user",
            principal="user:ada",
        ),
    )
    context.config.artifacts.retention_days = 1
    context.state.execute("UPDATE saved_artifacts SET created_at='2020-01-01T00:00:00Z'")
    assert artifact_lifecycle.run_retention(context)["archived"] == 1
    assert artifacts.get(context, draft["id"], "user:ada")["archived_at"] is not None


def test_deleting_imported_job_history_removes_its_redundant_legacy_snapshot(context, tmp_path):
    from pheasant.job_snapshots import JobSnapshotStore
    from pheasant.persistence.durable_jobs import store_for

    legacy = JobSnapshotStore(tmp_path / "jobs", 20)
    legacy.write({"id": "imported", "active": False, "finished_at": "2020-01-01T00:00:00Z"})
    imported = store_for(context.state, context.config.knowledge_base_id, 30, legacy.path, 20)
    assert imported.get("imported")
    assert (legacy.path / "imported.json.migrated").exists()
    imported.clear_finished("imported")
    assert not list(legacy.path.iterdir())


def test_invalid_saved_replacement_leaves_the_original_publication_unchanged(context):
    from pheasant.services.errors import ArtifactNotFound, InvalidRequest

    context.config.security.acl_enforced = True
    kept = artifacts.save(
        context,
        artifacts.SaveRequest(
            markdown="# Kept",
            principal="user:ada",
            scope="user",
        ),
    )
    kb = context.config.knowledge_base_id
    before = publications.status(context.state, kb, kept["id"])
    with pytest.raises(ArtifactNotFound):
        artifacts.save(
            context,
            artifacts.SaveRequest(
                markdown="# Forged",
                principal="user:bea",
                scope="user",
                replaces=kept["id"],
            ),
        )
    with pytest.raises(InvalidRequest, match="draft"):
        artifacts.save(
            context,
            artifacts.SaveRequest(
                markdown="# Draft",
                principal="user:ada",
                scope="user",
                replaces=kept["id"],
                published=False,
            ),
        )
    assert publications.status(context.state, kb, kept["id"]) == before


def test_idle_registry_is_not_kept_alive_by_its_heartbeat(context):
    import gc
    import weakref

    registry = JobRegistry(state=context.state, kb_id=context.config.knowledge_base_id)
    reference = weakref.ref(registry)
    del registry
    gc.collect()
    assert reference() is None


def test_diagram_numbers_and_visual_only_evidence_join_the_report():
    payload = evidence.normalize(
        {
            "answer": "Claim [9].",
            "citations": [{"index": 9, "source": "docs", "path": "a.md"}],
            "visual_citations": [{"index": 1, "source": "docs", "path": "b.md"}],
            "visual": {"status": "ok", "diagram": {"nodes": [{"id": "b", "cites": [1]}]}},
        }
    )
    assert payload["answer"] == "Claim [1]."
    assert payload["visual"]["diagram"]["nodes"][0]["cites"] == [2]
    assert payload["visual_citations"][0]["index"] == 2
    assert payload["visual_citations"][0]["evidence_id"] == payload["citations"][1]["evidence_id"]


def test_thread_diagram_uses_global_numbers_without_changing_turn_text():
    payload = evidence.normalize(
        {
            "answer": "First [1], second [2].",
            "citations": [
                {"index": 1, "source": "docs", "path": "a.md"},
                {"index": 2, "source": "docs", "path": "b.md"},
            ],
            "visual_citations": [{"index": 1, "source": "docs", "path": "b.md"}],
            "visual": {"status": "ok", "diagram": {"nodes": [{"id": "b", "cites": [1]}]}},
        }
    )
    assert payload["visual"]["diagram"]["nodes"][0]["cites"] == [2]
    assert payload["visual_citation_map"] == {"1": 2}


def test_evaluation_baseline_excludes_typed_and_saved_artifact_content(context):
    from pheasant.evaluation.contracts import EvaluatedQuery
    from pheasant.evaluation.replay import ReplayEngine
    from pheasant.evaluation.variants import default_matrix
    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.sqlite_store import SearchStore

    written = artifact_records.write(
        context,
        artifact_records.WriteRequest(
            "Zephyr artifact bridge uses the harbor subsystem.",
            scope="org",
        ),
    )
    artifacts.save(context, artifacts.SaveRequest(markdown="Zephyr artifact deployment report."))
    context.engine.sync_source("artifacts", "incremental")
    searcher = HybridSearch(SearchStore(context.state))
    replay = ReplayEngine(
        searcher, context.config.knowledge_base_id, graph=context.engine.graph_builder.graph
    )
    query = EvaluatedQuery("artifact-isolation", "Zephyr artifact")
    baseline, content = default_matrix()[:2]
    before = replay.replay_query(baseline, query)
    after = replay.replay_query(content, query)
    assert not before.failed and not after.failed
    # Global schema/group nodes may remain; no artifact content can enter B0.
    assert not before.memory_record_ids
    assert not any(
        "artifact-records" in identifier or "file:artifacts:" in identifier
        for identifier in before.ranked_ids
    )
    assert written["artifact_id"] in after.memory_record_ids
    assert any("file:artifacts:" in identifier for identifier in after.ranked_ids)


def test_promotion_failure_preserves_the_previous_overlay_and_output(context, monkeypatch):
    kb = context.config.knowledge_base_id
    first = retrieval_configurations.save_base(context, "first")
    tuning_store.apply_bundle(context.state, kb, first["bundle_id"], applied_by="test")
    before = publications.status(context.state, kb, "tuning:" + first["bundle_id"])
    context.config.search.ranking.rrf_k = 180.0
    second = retrieval_configurations.save_base(context, "second")

    def fail(*args, **kwargs):
        raise RuntimeError("publication storage unavailable")

    monkeypatch.setattr(publications, "publish", fail)
    with pytest.raises(RuntimeError, match="unavailable"):
        tuning_store.apply_bundle(context.state, kb, second["bundle_id"], applied_by="test")
    assert tuning_store.active_overlay(context.state, kb)["bundle_id"] == first["bundle_id"]
    assert publications.status(context.state, kb, "tuning:" + first["bundle_id"]) == before


@pytest.mark.parametrize("marker", ["5", "current"])
def test_existing_schema_marker_cannot_skip_artifact_upgrade(context, marker):
    """An old fleet must migrate before source adoption or publication reads."""
    from pheasant.persistence.state_store import SCHEMA_VERSION

    saved = artifacts.save(context, artifacts.SaveRequest(markdown="# Preserved output"))
    kb = context.config.knowledge_base_id
    payload = artifacts.get(context, saved["id"])["payload"]
    for table in (
        "durable_jobs",
        "artifact_source_migrations",
        "artifact_publications",
        "artifact_versions",
    ):
        context.state.conn.execute(f"DROP TABLE {table}")
    context.state.conn.execute("ALTER TABLE saved_artifacts DROP COLUMN publication_state")
    context.state.conn.execute(
        "INSERT INTO pheasant_schema_meta(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("core", SCHEMA_VERSION if marker == "current" else marker, "2026-01-01T00:00:00Z"),
    )
    context.state.conn.commit()

    context.state.migrate()
    context.state.migrate()

    assert artifacts.get(context, saved["id"])["payload"] == payload
    assert publications.status(context.state, kb, saved["id"])["status"] == "published"
    written = artifact_records.write(context, artifact_records.WriteRequest("After upgrade"))
    assert written["artifact_id"]
    jobs = JobRegistry(state=context.state, kb_id=kb)
    try:
        job = jobs.create("evaluation", "After upgrade")
        jobs.finish(job.id, result={"artifact_id": saved["id"]})
        assert jobs.get(job.id)["result"]["artifact_id"] == saved["id"]
    finally:
        jobs.close()


def test_auto_record_taxonomy_keeps_atomic_chunk_ids(context):
    from pheasant.ingestion.pipeline import _chunks_and_headings
    from pheasant.sync.fingerprint import source_fingerprint

    source = artifact_records.source(context, create=True)
    source.chunking.strategy = "auto"
    text = "# Bridge\n\n## Retention\nKeep the published output.\n"
    source.taxonomy.enabled = False
    old_chunks, old_headings, _ = _chunks_and_headings(source, text, "org/record.md")
    old_fingerprint = source_fingerprint(source)
    assert not old_headings
    source.taxonomy.enabled = True
    chunks, headings, _ = _chunks_and_headings(source, text, "org/record.md")
    assert [h.title for h in headings] == ["Bridge", "Retention"]
    assert [c.text_hash for c in chunks] == [c.text_hash for c in old_chunks]
    assert source_fingerprint(source) != old_fingerprint


def test_qualified_heading_reads_are_bounded_in_the_row_store(context, monkeypatch):
    from pheasant.graph.sql import SqlGraph

    graph = SqlGraph(context.state, context.config.knowledge_base_id)
    graph.rows.apply_delta(
        graph.kb_id,
        node_upserts=[
            (
                f"heading:{source}:{i}",
                {
                    "type": "heading",
                    "source_id": source,
                    "relative_path": "guide.md",
                    "title": str(i),
                },
            )
            for source in ("one", "two")
            for i in range(5)
        ]
        + [("file:one:noise", {"type": "file", "source_id": "one"})],
    )

    def no_scan(*args, **kwargs):
        raise AssertionError("a qualified outline must not scan the full graph")

    monkeypatch.setattr(graph.rows, "iter_nodes", no_scan)
    found = list(
        graph.filtered_nodes(
            node_type="heading", source_id="two", relative_path="guide.md", limit=3
        )
    )
    assert len(found) == 3
    assert all(attrs["source_id"] == "two" for _, attrs in found)
    assert not list(graph.filtered_nodes(node_type="heading", relative_path="missing.md"))


def test_artifact_coverage_reads_only_the_record_frontier(context, monkeypatch):
    from pheasant.graph.sql import SqlGraph
    from pheasant.services.artifact_graph import coverage

    graph = SqlGraph(context.state, context.config.knowledge_base_id)
    graph.rows.apply_delta(
        graph.kb_id,
        edge_upserts=[
            ("record", "heading", 0, {"type": "about", "match_signal": "subject"}),
            ("record", "other", 0, {"type": "contains"}),
            ("noise", "heading", 0, {"type": "about", "match_signal": "subject"}),
        ],
    )

    def no_scan(*args, **kwargs):
        raise AssertionError("record coverage must not scan the region's edges")

    monkeypatch.setattr(graph, "iter_edges", no_scan)
    assert coverage(graph, ["record", "unlinked", "record"]) == {
        "records": 2,
        "bridged": 1,
        "unbridged": 1,
        "by_signal": {"subject": 1},
    }


def test_readonly_notification_cleanup_does_not_fail_durable_deletion(
    context, tmp_path, monkeypatch
):
    import errno
    from pathlib import Path

    from pheasant.job_snapshots import JobSnapshotStore

    legacy = JobSnapshotStore(tmp_path / "jobs", 10)
    legacy.path.mkdir()
    migrated = legacy.path / "done.json.migrated"
    migrated.write_text("{}")
    store = DurableJobStore(context.state, context.config.knowledge_base_id, legacy=legacy)
    store.write({"id": "done", "active": False, "finished_at": "2026-01-01T00:00:00Z"})
    unlink = Path.unlink

    def readonly(path, *args, **kwargs):
        if path == migrated:
            raise OSError(errno.EROFS, "read-only NFS mount")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "unlink", readonly)
        assert store.clear_finished("done") == {"done"}
    assert store.get("done") is None and migrated.exists()
    # The notification is handled in another process after the row is gone.
    assert store.clear_finished("done") == set()
    assert not migrated.exists()


def test_retirement_is_hidden_while_the_fleet_writer_is_deferred(context):
    from dataclasses import replace

    from pheasant.evaluation.contracts import EvaluatedQuery
    from pheasant.evaluation.replay import ReplayEngine
    from pheasant.evaluation.variants import default_matrix
    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.sqlite_store import SearchStore
    from pheasant.services.archive import Indexing

    written = artifact_records.write(
        context, artifact_records.WriteRequest("Zephyrcode retired artifact.", scope="org")
    )
    replay = ReplayEngine(
        HybridSearch(SearchStore(context.state)),
        context.config.knowledge_base_id,
        graph=context.engine.graph_builder.graph,
        mode="text",
    )
    query = EvaluatedQuery("retirement", "Zephyrcode")
    variant = default_matrix()[1]
    assert written["artifact_id"] in replay.replay_query(variant, query).memory_record_ids
    queued = []
    deferred = replace(
        context,
        indexing=Indexing(
            local=False,
            start_sync=lambda source: {},
            remove_documents=lambda source: queued.append(source) or {},
        ),
    )
    artifact_records.retire(deferred, written["artifact_id"])
    assert queued
    assert context.state.rows(
        "SELECT valid_until FROM memory_records WHERE record_id=?", (written["artifact_id"],)
    )[0]["valid_until"]
    assert context.state.rows("SELECT id FROM artifacts")  # writer has not pruned yet
    assert not replay.replay_query(variant, query).ranked_ids
    assert (
        publications.status(
            context.state, context.config.knowledge_base_id, written["artifact_id"]
        )["status"]
        == "deprecated"
    )
