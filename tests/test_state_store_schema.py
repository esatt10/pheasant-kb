from __future__ import annotations

from pheasant.persistence.state_store import StateStore


def test_artifact_term_index_migration_removes_the_unbounded_node_id_key(tmp_path) -> None:
    """Existing state must drop only the unsafe derived index, never term rows."""

    store = StateStore(tmp_path / "state.db")
    try:
        store.migrate()
        store.conn.execute("DROP INDEX idx_artifact_terms_type_artifact")
        store.conn.execute(
            "CREATE INDEX idx_artifact_terms_node_lookup "
            "ON artifact_terms(node_type, node_id, artifact_id)"
        )
        store.conn.execute(
            "DELETE FROM pheasant_schema_meta WHERE key=?", ("artifact_terms_node_lookup_v2",)
        )
        store.conn.commit()

        store.migrate()

        indexes = {
            row["name"]
            for row in store.rows("SELECT name FROM sqlite_master WHERE type='index'", ())
        }
        assert "idx_artifact_terms_node_lookup" not in indexes
        assert "idx_artifact_terms_type_artifact" in indexes
    finally:
        store.close()
