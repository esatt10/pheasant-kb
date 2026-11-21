"""Public connector conformance harness (Product Framework Step 31.1).

Third-party connector packages subclass :class:`ConnectorConformance` in
their own pytest suite, implement :meth:`make_connector`, and get the
pheasant connector contract asserted for free — the same bar the built-in
connectors are held to in this repo's test suite:

    from pheasant.testing import ConnectorConformance

    class TestMyConnectorConformance(ConnectorConformance):
        def make_connector(self, tmp_path, state):
            root = tmp_path / "content"
            root.mkdir()
            (root / "a.txt").write_text("alpha")
            source = SourceConfig(name="demo", type=PluginSourceType("mytype"), path=root)
            return MyConnector(source, state)

The contract asserted here is what the sync engine relies on: a declared
``connector_type``, a healthy ``validate()``, deterministic + unique item
identities, stable payload bytes for unchanged content (or an explicit
``ItemNotModified``), a JSON-serializable checkpoint that round-trips
through the state store, and ``full`` mode ignoring the checkpoint.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pheasant.persistence.state_store import StateStore
from pheasant.sync.connectors import ConnectorItem, ItemNotModified, SourceConnector


class ConnectorConformance:
    """Subclass with one factory method; every ``test_*`` below runs."""

    def make_connector(self, tmp_path: Path, state: StateStore) -> SourceConnector:
        """Build a connector over sample content created under ``tmp_path``."""
        raise NotImplementedError("conformance subclasses implement make_connector()")

    def make_state(self, tmp_path: Path) -> StateStore:
        """The state store: the PostgreSQL database ``PHEASANT_DATABASE_URL`` names."""

        state = StateStore()
        state.migrate()
        return state

    def _connector(self, tmp_path: Path) -> SourceConnector:
        state = self.make_state(tmp_path)
        connector = self.make_connector(tmp_path, state)
        # A checkpoint belongs to a registered source (a foreign key).
        source = connector.source
        state.upsert_knowledge_base("conformance", "conformance", None, "conformance", "")
        state.upsert_source(
            source.name,
            "conformance",
            source.name,
            str(getattr(source.type, "value", source.type)),
            str(getattr(source, "path", "") or ""),
            True,
            {},
        )
        return connector

    @staticmethod
    def _payload_sha(connector: SourceConnector, item: ConnectorItem) -> str | None:
        try:
            payload = connector.read_item(item)
        except ItemNotModified:
            return None
        return hashlib.sha256(payload.content).hexdigest()

    def test_declares_a_connector_type(self, tmp_path: Path) -> None:
        connector = self._connector(tmp_path)
        assert isinstance(connector.connector_type, str)
        assert connector.connector_type and connector.connector_type != "base"

    def test_validate_reports_health(self, tmp_path: Path) -> None:
        health = self._connector(tmp_path).validate()
        assert health.ok, health.errors
        assert health.item_count >= 1, "conformance content must yield at least one item"

    def test_list_items_is_deterministic_with_unique_identities(self, tmp_path: Path) -> None:
        connector = self._connector(tmp_path)
        first = connector.list_items()
        second = connector.list_items()
        assert first, "make_connector() must create indexable sample content"
        identities = [item.identity for item in first]
        assert all(identities), "every item needs a non-empty identity"
        assert len(set(identities)) == len(identities), "identities must be unique"
        assert identities == [item.identity for item in second]
        assert [i.relative_path for i in first] == [i.relative_path for i in second]

    def test_unchanged_payloads_are_stable(self, tmp_path: Path) -> None:
        connector = self._connector(tmp_path)
        for item in connector.list_items():
            first = self._payload_sha(connector, item)
            second = self._payload_sha(connector, item)
            # Either stable bytes, or the connector signals not-modified —
            # both are contract-conformant idempotency signals.
            assert first == second or second is None

    def test_checkpoint_round_trips_through_the_state_store(self, tmp_path: Path) -> None:
        connector = self._connector(tmp_path)
        items = connector.list_items()
        cursor, high_watermark = connector.checkpoint_from_items(items)
        json.dumps({"cursor": cursor, "high_watermark": high_watermark})  # serializable
        connector.set_checkpoint(cursor, high_watermark, "healthy")
        stored = connector.get_checkpoint()
        assert stored is not None
        assert stored.get("cursor") == cursor
        assert stored.get("high_watermark") == high_watermark
        connector.begin_sync("incremental")  # must consume its own checkpoint

    def test_full_mode_ignores_the_checkpoint(self, tmp_path: Path) -> None:
        connector = self._connector(tmp_path)
        cursor, high_watermark = connector.checkpoint_from_items(connector.list_items())
        connector.set_checkpoint(cursor, high_watermark, "healthy")
        connector.begin_sync("full")
        assert connector._previous_cursor is None
        assert connector._previous_watermark is None
