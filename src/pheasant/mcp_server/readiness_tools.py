"""The readiness plane's half of the MCP facade.

A mixin rather than more lines in `tools.py`, which the module ratchet in
`tests/test_module_budget.py` is explicit about: that file is one every
feature touches, and the fix it asks for is a split by bounded context rather
than a raised ceiling. This is one — eight adapters that all serve
`docs/stress-test-readiness.md` and nothing else.

`PheasantTools` inherits it, so the public tool surface is unchanged: every
name here is reachable exactly where it was, which rule 8 requires and
`tests/test_surface_conformance.py` checks. What changed is which file a
reader opens to find them.

Adapters, like the rest of the facade: parse a request, call one service
function, marshal the answer. Nothing here decides anything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pheasant.config.schema import SourceConfig, SourceType
from pheasant.registry.source_registry import SourceRegistry


class ReadinessTools:
    """Contract, submission, receipts and snapshots, over MCP."""

    # Supplied by `PheasantTools`. Declared so a reader can see what this mixin
    # depends on rather than discovering it from an AttributeError, and so a
    # type checker has something to work with — a mixin whose collaborators are
    # implicit is a mixin nobody can move.
    config: Any
    engine: Any
    services: Any
    state: Any

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:  # pragma: no cover
        raise NotImplementedError

    def get_readiness_contract(self, knowledge_base: str | None = None) -> dict:
        """What this build supports, with a digest a harness can pin.

        Read this *before* hard-coding a tool name or assuming a capability.
        Every row says which gap in ``docs/stress-test-readiness.md`` it closes
        and whether it is `proven`, `supported`, `declared_untested` or
        `unsupported` — and an `unsupported` row carries the reason, because a
        harness needs to tell "this region cannot" from "this region did not
        mention it".
        """

        if knowledge_base:
            self._require_knowledge_base(knowledge_base)
        from pheasant.readiness.contract import build_contract

        return build_contract(self.config)

    def run_readiness_check(
        self,
        knowledge_base: str | None = None,
        gate_sets: list[str] | None = None,
    ) -> dict:
        """Probe this region and return the go/no-go verdict.

        Performs real work: it submits documents to a scratch source it owns,
        indexes them, seals snapshots and runs searches. It never writes to a
        configured source and never writes memory.
        """

        if knowledge_base:
            self._require_knowledge_base(knowledge_base)
        from pheasant.readiness.gates import GATE_SETS
        from pheasant.readiness.runner import run_readiness_check as _run

        return _run(
            self.services,
            sync=self._readiness_sync,
            gate_sets=tuple(gate_sets or GATE_SETS),
        )

    def _readiness_sync(self, source_name: str) -> dict:
        """Index the readiness plane's scratch source, registering it first.

        Registration is idempotent (`upsert_source`), so a repeated check
        reuses one scratch source rather than accumulating one per run — the
        same reason the probe source name is fixed rather than minted.
        """

        from pheasant.readiness.probes import PROBE_SOURCE

        if source_name != PROBE_SOURCE:
            raise ValueError(f"the readiness plane only indexes {PROBE_SOURCE}")
        from pheasant.ingestion.landing import upload_root

        directory = upload_root(Path(self.config.pheasant.state_path), source_name)
        if not any(source.name == source_name for source in self.config.sources):
            # Registered exactly as `register_source` does it — through the
            # registry, then onto `config.sources` — rather than by a shortcut
            # of its own. A readiness check that indexed through a private path
            # would be demonstrating that private path.
            source = SourceConfig(
                name=source_name,
                type=SourceType.document_folder,
                path=directory,
                description="Readiness probe scratch source. Safe to delete.",
                enabled=True,
            )
            SourceRegistry(self.config, self.state).register_source(source)
            self.config.sources.append(source)
        return self.engine.sync_source(source_name, mode="incremental")

    def submit_documents(
        self,
        knowledge_base: str,
        documents: list[dict],
        source_name: str = "submissions",
        submission_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict:
        """Persist documents with an idempotency key and a receipt per item.

        Each entry in ``documents`` is ``{"relative_path": str, "text": str,
        "idempotency_key": str | None, "metadata": dict | None}``. A retry
        carrying a key this region has already seen folds onto the receipt it
        wrote rather than producing a second copy.

        Acceptance is **not** searchability. Sync the source, then call
        ``acknowledge_ingest`` to cross the index barrier.
        """

        self._require_knowledge_base(knowledge_base)
        from pheasant.services import ingestion as ingestion_service

        items = [
            ingestion_service.SubmissionItem(
                relative_path=str(entry.get("relative_path") or ""),
                content=str(entry.get("text") or "").encode("utf-8"),
                idempotency_key=entry.get("idempotency_key"),
                metadata=dict(entry.get("metadata") or {}),
            )
            for entry in documents or []
        ]
        return ingestion_service.submit(
            self.services,
            ingestion_service.SubmissionRequest(
                items=items,
                source_name=source_name,
                knowledge_base=knowledge_base,
                submission_id=submission_id,
                agent_id=agent_id,
            ),
        )

    def get_ingest_status(
        self,
        knowledge_base: str,
        idempotency_key: str | None = None,
        submission_id: str | None = None,
    ) -> dict:
        """Receipts: accepted, indexed, rejected or failed, per submitted item."""

        from pheasant.services import ingestion as ingestion_service

        return ingestion_service.ingest_status(
            self.services,
            knowledge_base,
            idempotency_key=idempotency_key,
            submission_id=submission_id,
        )

    def acknowledge_ingest(self, knowledge_base: str, submission_id: str | None = None) -> dict:
        """Cross the index barrier for receipts whose artifacts now exist."""

        from pheasant.services import ingestion as ingestion_service

        return ingestion_service.acknowledge_indexed(self.services, knowledge_base, submission_id)

    def reconcile_ingest(self, knowledge_base: str, submission_id: str | None = None) -> dict:
        """Submitted against held, with the silent-loss count named."""

        from pheasant.services import ingestion as ingestion_service

        return ingestion_service.reconcile(self.services, knowledge_base, submission_id)

    def seal_snapshot(
        self,
        knowledge_base: str,
        label: str | None = None,
        sealed_by: str | None = None,
        note: str | None = None,
    ) -> dict:
        """Seal the current state as the reference snapshot for a run.

        Idempotent over an unchanged region: the id is a digest of the state,
        so sealing twice returns one snapshot rather than two.
        """

        from pheasant.services import snapshots as snapshot_service

        return snapshot_service.seal(
            self.services,
            snapshot_service.SealRequest(
                knowledge_base=knowledge_base, label=label, sealed_by=sealed_by, note=note
            ),
        )

    def get_snapshot(self, knowledge_base: str, snapshot_id: str) -> dict:
        """One snapshot's manifest, and whether the region still stands there."""

        self._require_knowledge_base(knowledge_base)
        from pheasant.services import snapshots as snapshot_service

        payload = snapshot_service.describe(self.services, snapshot_id)
        payload["verification"] = snapshot_service.verify(self.services, snapshot_id)
        return payload

    def list_snapshots(self, knowledge_base: str, limit: int = 50) -> dict:
        """Every snapshot this region holds, saying which are sealed."""

        from pheasant.services import snapshots as snapshot_service

        return snapshot_service.catalogue(self.services, knowledge_base, limit=limit)


def register_readiness_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    """Register the readiness plane's tools on an MCP server.

    Handed `mcp` and `anticipated` rather than importing them, for two
    reasons. The decorator is built per surface in `server.py` — a `ToolError`
    raised inside a resource handler is stripped exactly like a crash, so the
    tool and resource variants are not interchangeable — and this module must
    not import the SDK, which is what keeps `ReadinessTools` above usable by
    the HTTP surface and by a test with no MCP installed.

    Every docstring here is read by an agent, so they say what the tool does
    *and* what it does not: `seal_snapshot`'s promise is a refusal rather than
    time travel, and an agent that assumes otherwise will ingest during a run.
    """

    @mcp.tool()
    @anticipated
    def get_readiness_contract(knowledge_base: str | None = None) -> dict:
        """Report what this build supports, with a digest a harness can pin.

        Read this before assuming a tool name or a capability. Every row names
        the readiness gap it closes and reports `proven`, `supported`,
        `declared_untested` or `unsupported` — and an unsupported row carries
        its reason, because "this region cannot" and "this region did not
        mention it" call for different responses.

        The `refusal_codes` table is how an MCP client maps a refusal's text
        onto a machine-readable code: this transport carries a string and
        nothing else, so the mapping is published rather than inlined.
        """

        return tools.get_readiness_contract(knowledge_base)

    @mcp.tool()
    @anticipated
    def run_readiness_check(
        knowledge_base: str | None = None,
        gate_sets: list[str] | None = None,
    ) -> dict:
        """Probe this region and return the go/no-go verdict per gate set.

        Performs real work: submits documents to a scratch source it owns,
        indexes them, seals snapshots and runs searches. It never writes to a
        configured source and never writes memory.
        """

        return tools.run_readiness_check(knowledge_base, gate_sets)

    @mcp.tool()
    @anticipated
    def submit_documents(
        knowledge_base: str,
        documents: list[dict],
        source_name: str = "submissions",
        submission_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict:
        """Persist documents with an idempotency key and one receipt per item.

        Each entry is ``{"relative_path", "text", "idempotency_key"?,
        "metadata"?}``. Re-submitting under a key this region has already seen
        folds onto the receipt it wrote rather than making a second copy.

        Acceptance is not searchability: sync the source, then call
        `acknowledge_ingest`.
        """

        return tools.submit_documents(
            knowledge_base, documents, source_name, submission_id, agent_id
        )

    @mcp.tool()
    @anticipated
    def get_ingest_status(
        knowledge_base: str,
        idempotency_key: str | None = None,
        submission_id: str | None = None,
    ) -> dict:
        """Return receipts: accepted, indexed, rejected or failed, per item."""

        return tools.get_ingest_status(knowledge_base, idempotency_key, submission_id)

    @mcp.tool()
    @anticipated
    def acknowledge_ingest(knowledge_base: str, submission_id: str | None = None) -> dict:
        """Cross the index barrier for receipts whose artifacts now exist."""

        return tools.acknowledge_ingest(knowledge_base, submission_id)

    @mcp.tool()
    @anticipated
    def reconcile_ingest(knowledge_base: str, submission_id: str | None = None) -> dict:
        """Reconcile submissions against what the region holds.

        ``silent_loss`` is the number worth reading: receipts claiming an
        artifact this region does not have.
        """

        return tools.reconcile_ingest(knowledge_base, submission_id)

    @mcp.tool()
    @anticipated
    def seal_snapshot(
        knowledge_base: str,
        label: str | None = None,
        sealed_by: str | None = None,
        note: str | None = None,
    ) -> dict:
        """Seal the current state as a run's reference snapshot.

        Idempotent over an unchanged region: the id is a digest of the state.
        Pin a search to the returned `snapshot_id` and this region will answer
        from that state or refuse — it does not hold older corpus versions, so
        the guarantee is that two runs naming one snapshot cannot silently have
        seen different corpora.
        """

        return tools.seal_snapshot(knowledge_base, label, sealed_by, note)

    @mcp.tool()
    @anticipated
    def get_snapshot(knowledge_base: str, snapshot_id: str) -> dict:
        """Return a snapshot's manifest and whether the region still stands there."""

        return tools.get_snapshot(knowledge_base, snapshot_id)

    @mcp.tool()
    @anticipated
    def list_snapshots(knowledge_base: str, limit: int = 50) -> dict:
        """Return every snapshot this region holds, saying which are sealed."""

        return tools.list_snapshots(knowledge_base, limit)
