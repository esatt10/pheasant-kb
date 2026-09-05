"""The readiness plane's HTTP routes.

Split out of `api/app.py` rather than added to it. That file is the module
ratchet's headline and its own docstring names the fix — one router per plane,
which the review said would be easier once an application-service layer
existed. It does now for these operations: every handler below parses a
request, calls one function in `services/`, and marshals the answer, so there
is nothing here that a second implementation could drift from.

`register_readiness_routes` takes the collaborators rather than reaching for
them, for the reason `ServiceContext` does: a registration function that is
handed what it needs can be driven by `create_app`, by a test, and by any
future composition without any of them knowing about the others.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from pheasant.config.schema import SourceConfig, SourceType
from pheasant.registry.source_registry import SourceRegistry
from pheasant.services import ingestion as ingestion_service
from pheasant.services import snapshots as snapshot_service


class ReadinessCheckRequest(BaseModel):
    """Which gate sets to evaluate. Empty means all four."""

    gate_sets: list[str] | None = None


class SubmittedDocument(BaseModel):
    relative_path: str
    text: str
    #: Absent means content-addressed: a digest over (relative_path, text).
    #: A caller that does not supply one still gets idempotency rather than a
    #: fresh row per attempt.
    idempotency_key: str | None = None
    metadata: dict | None = None


class SubmitDocumentsRequest(BaseModel):
    documents: list[SubmittedDocument]
    source_name: str = "submissions"
    knowledge_base: str | None = None
    submission_id: str | None = None
    agent_id: str | None = None


class SealSnapshotRequest(BaseModel):
    knowledge_base: str | None = None
    label: str | None = None
    sealed_by: str | None = None
    note: str | None = None


def register_readiness_routes(app: FastAPI, *, config: Any, services: Any, engine: Any) -> None:
    """Register the readiness, ingestion and snapshot routes on ``app``.

    These are adapters over `services.ingestion`, `services.snapshots` and
    `pheasant.readiness` — the same operations the MCP tools call, so a harness
    gets one answer whichever transport it uses.
    """

    @app.get("/readiness/contract")
    def readiness_contract() -> dict:
        """What this build supports, with a digest a harness can pin.

        Served whether or not `readiness.enabled` is set: a harness's first
        question is "can this region be measured", and answering it with a 404
        is indistinguishable from an old build that has no contract at all.
        The payload reports `readiness_enabled` so the caller can tell.
        """

        from pheasant.readiness.contract import build_contract

        return build_contract(config)

    @app.post("/readiness/check")
    def readiness_check(req: ReadinessCheckRequest) -> dict:
        """Probe this region and return the go/no-go verdict per gate set.

        Gated on `readiness.enabled`, unlike the contract above, because this
        one *writes*: it submits documents to a scratch source, indexes them
        and seals snapshots. A region that has not opted in should not have a
        stranger's HTTP request start indexing.
        """

        if not config.readiness.enabled:
            raise HTTPException(
                status_code=409,
                detail=(
                    "readiness.enabled is off. A check submits documents, indexes them "
                    "and seals snapshots, so it is opt-in."
                ),
            )
        from pheasant.readiness.gates import GATE_SETS
        from pheasant.readiness.runner import run_readiness_check

        return run_readiness_check(
            services,
            sync=_readiness_sync,
            gate_sets=tuple(req.gate_sets or GATE_SETS),
        )

    def _readiness_sync(source_name: str) -> dict:
        """Index the readiness plane's scratch source, registering it first.

        Registration is idempotent, so repeated checks reuse one scratch source
        rather than accumulating one per run.
        """

        from pheasant.ingestion.landing import upload_root
        from pheasant.readiness.probes import PROBE_SOURCE

        if source_name != PROBE_SOURCE:
            raise ValueError(f"the readiness plane only indexes {PROBE_SOURCE}")
        directory = upload_root(Path(config.pheasant.state_path), source_name)
        if not any(source.name == source_name for source in config.sources):
            # Registered through the registry, exactly as `POST /sources` does
            # it. A readiness check that indexed through a private path would
            # be demonstrating that private path.
            source = SourceConfig(
                name=source_name,
                type=SourceType.document_folder,
                path=directory,
                description="Readiness probe scratch source. Safe to delete.",
                enabled=True,
            )
            SourceRegistry(config, services.state).register_source(source)
            config.sources.append(source)
        return engine.sync_source(source_name, mode="incremental")

    @app.post("/ingest/submit")
    def ingest_submit(req: SubmitDocumentsRequest) -> dict:
        """Persist documents with an idempotency key and a receipt per item."""

        items = [
            ingestion_service.SubmissionItem(
                relative_path=entry.relative_path,
                content=entry.text.encode("utf-8"),
                idempotency_key=entry.idempotency_key,
                metadata=entry.metadata or {},
            )
            for entry in req.documents
        ]
        return ingestion_service.submit(
            services,
            ingestion_service.SubmissionRequest(
                items=items,
                source_name=req.source_name,
                knowledge_base=req.knowledge_base,
                submission_id=req.submission_id,
                agent_id=req.agent_id,
            ),
        )

    @app.get("/ingest/status")
    def ingest_status(
        knowledge_base: str | None = None,
        idempotency_key: str | None = None,
        submission_id: str | None = None,
    ) -> dict:
        """Receipts: accepted, indexed, rejected or failed, per submitted item."""

        return ingestion_service.ingest_status(
            services,
            knowledge_base,
            idempotency_key=idempotency_key,
            submission_id=submission_id,
        )

    @app.post("/ingest/acknowledge")
    def ingest_acknowledge(
        knowledge_base: str | None = None, submission_id: str | None = None
    ) -> dict:
        """Cross the index barrier for receipts whose artifacts now exist."""

        return ingestion_service.acknowledge_indexed(services, knowledge_base, submission_id)

    @app.get("/ingest/reconcile")
    def ingest_reconcile(
        knowledge_base: str | None = None, submission_id: str | None = None
    ) -> dict:
        """Submitted against held, with the silent-loss count named."""

        return ingestion_service.reconcile(services, knowledge_base, submission_id)

    @app.post("/snapshots/seal")
    def snapshot_seal(req: SealSnapshotRequest) -> dict:
        """Seal the current state as a run's reference snapshot."""

        return snapshot_service.seal(
            services,
            snapshot_service.SealRequest(
                knowledge_base=req.knowledge_base,
                label=req.label,
                sealed_by=req.sealed_by,
                note=req.note,
            ),
        )

    @app.get("/snapshots")
    def snapshot_list(knowledge_base: str | None = None, limit: int = 50) -> dict:
        """Every snapshot this region holds, saying which are sealed."""

        return snapshot_service.catalogue(services, knowledge_base, limit=limit)

    @app.get("/snapshots/{snapshot_id}")
    def snapshot_get(snapshot_id: str) -> dict:
        """One snapshot's manifest, and whether the region still stands there."""

        payload = snapshot_service.describe(services, snapshot_id)
        payload["verification"] = snapshot_service.verify(services, snapshot_id)
        return payload
