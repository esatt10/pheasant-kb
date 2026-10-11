from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from itertools import islice

from pheasant.config.loader import config_hash
from pheasant.config.schema import PheasantConfig, SourceConfig
from pheasant.ingestion.landing import owned_upload_directory
from pheasant.persistence.state_store import StateStore

logger = logging.getLogger(__name__)


def now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class SourceRegistry:
    def __init__(self, config: PheasantConfig, state: StateStore):
        self.config = config
        self.state = state

    def initialize(self) -> None:
        self.state.upsert_knowledge_base(
            self.config.knowledge_base_id,
            self.config.pheasant.name,
            self.config.pheasant.description,
            config_hash(self.config),
            now(),
        )
        for source in self.config.sources:
            if not self.state.source_removed(source.name):
                upload_dir = owned_upload_directory(
                    self.config.pheasant.state_path,
                    source.name,
                    source.type.value,
                    source.path,
                )
                if source.enabled and upload_dir is not None and not upload_dir.is_dir():
                    # A configured upload source exists before its first file.
                    # On a fresh state volume its owned directory must exist
                    # before startup sync or it is reported as path_missing.
                    # Serving replicas may mount /state read-only; the writer
                    # (or db-init) creates it, so a read-only replica can defer.
                    try:
                        upload_dir.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        logger.warning(
                            "could not create configured upload directory %s",
                            upload_dir,
                            exc_info=True,
                        )
                # The UI-owned landing zone accepts every supported document.
                # A generated config may name that same source with the
                # code-shaped default include list. On restart the indexer
                # would otherwise see no ZIP/PDF and prune their indexed rows.
                if upload_dir is not None and "**/*" not in source.include:
                    source.include = ["**/*"]
                self.register_source(source, revive=False)

    def register_source(self, source: SourceConfig, *, revive: bool = True) -> None:
        self.state.upsert_source(
            source.name,
            self.config.knowledge_base_id,
            source.name,
            source.type.value,
            str(source.path),
            source.enabled,
            source.model_dump(mode="json"),
            clear_removal=revive,
        )

    def list_sources(
        self,
        enabled: bool | None = None,
        status: str | None = None,
        source_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        checkpoints = {
            checkpoint["source_id"]: checkpoint
            for checkpoint in self.state.list_source_checkpoints()
        }
        where = [
            "NOT EXISTS (SELECT 1 FROM removed_sources "
            "WHERE removed_sources.source_id = sources.id)"
        ]
        params: list[object] = []
        if enabled is not None:
            where.append("enabled=?")
            params.append(int(enabled))
        if status:
            where.append("last_status=?")
            params.append(status)
        if source_type:
            where.append("type=?")
            params.append(source_type)
        clause = "WHERE " + " AND ".join(where) if where else ""
        params.extend([limit, offset])
        sources = []
        for row in self.state.rows(
            f"SELECT * FROM sources {clause} ORDER BY name LIMIT ? OFFSET ?",
            tuple(params),
        ):
            source = dict(row)
            source["checkpoint"] = checkpoints.get(source["id"])
            upload_dir = owned_upload_directory(
                self.config.pheasant.state_path,
                str(source["name"]),
                str(source["type"]),
                str(source["path"]),
            )
            if upload_dir is not None and upload_dir.is_dir():
                try:
                    source["uploaded_archives"] = sorted(
                        entry.name
                        for entry in islice(upload_dir.iterdir(), 1000)
                        if entry.is_file() and entry.suffix.lower() == ".zip"
                    )[:20]
                except OSError:
                    source["uploaded_archives"] = []
            # URL-backed repositories carry commit evidence in their latest
            # checkpoint. Promote it to a stable source-status field so the UI
            # and MCP clients can answer the operational question directly:
            # remote == checkout == indexed commit?
            try:
                configured = json.loads(source.get("config_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                configured = {}
            repo = configured.get("repo") if isinstance(configured, dict) else None
            if isinstance(repo, dict) and repo.get("clone_url"):
                checkpoint = source.get("checkpoint") or {}
                high_watermark = checkpoint.get("high_watermark") or {}
                evidence = dict(high_watermark.get("repository") or {})
                evidence.setdefault("managed", True)
                evidence.setdefault("remote_url", repo.get("clone_url"))
                evidence.setdefault("requested_ref", repo.get("clone_ref"))
                evidence["fresh"] = bool(
                    evidence.get("fresh") and source.get("last_status") == "healthy"
                )
                source["repository"] = evidence
            sources.append(source)
        return sources
