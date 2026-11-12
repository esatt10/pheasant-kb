"""Apply durable artifact file intents on the writing tier only."""

from __future__ import annotations

import json
from typing import Any

from pheasant.config.schema import PheasantConfig
from pheasant.ingestion.landing_service import LocalLandingZone
from pheasant.persistence import artifact_outbox
from pheasant.registry.source_registry import SourceRegistry
from pheasant.sync import archived


def apply(engine: Any) -> tuple[int, bool, list[dict]]:
    kb = engine.config.knowledge_base_id
    tasks = artifact_outbox.pending(engine.state, kb)
    if not tasks:
        return 0, False, []
    zone = LocalLandingZone(engine.paths.state)
    directory = zone.directory("artifacts")
    indexed = False
    completed = 0
    acknowledgments: list[dict] = []
    for task in tasks:
        try:
            with engine._source_write_lock("artifacts"), engine.state.conn:
                suffix = " FOR UPDATE" if engine.state.dialect.is_postgres else ""
                current = engine.state.rows(
                    "SELECT revision FROM artifact_outbox WHERE kb_id=? AND folder=?" + suffix,
                    (kb, task["folder"]),
                )
                if not current or current[0]["revision"] != task["revision"]:
                    continue
                payload = json.loads(task["payload_json"])
                if not engine.state.get_source("artifacts"):
                    source = PheasantConfig.model_validate(
                        {
                            "sources": [
                                {
                                    "name": "artifacts",
                                    "type": "document_folder",
                                    "path": directory,
                                    "include": ["**/*"],
                                    "sync": payload.get("pacing", {"on_startup": False}),
                                }
                            ]
                        }
                    ).sources[0]
                    SourceRegistry(engine.config, engine.state).register_source(source)
                if task["operation"] == "publish":
                    live = engine.state.rows(
                        "SELECT archived_at FROM saved_artifacts WHERE kb_id=? AND id=?",
                        (kb, payload["artifact_id"]),
                    )
                    if live and not live[0]["archived_at"]:
                        for name, content in payload["files"]:
                            zone.write(
                                "artifacts",
                                f"{task['folder']}/{name}",
                                content.encode("utf-8"),
                                unique=False,
                            )
                        indexed |= bool(payload.get("index", True))
            # The operation owns the same source lock; apply obtains it itself.
            if task["operation"] == "remove":
                archived.apply(engine, "artifacts")
            acknowledgments.append(task)
            completed += 1
        except Exception as exc:
            engine.state.execute(
                "UPDATE artifact_outbox SET last_error=? WHERE kb_id=? AND folder=? AND revision=?",
                (str(exc)[:1000], kb, task["folder"], task["revision"]),
            )
            raise
    return completed, indexed, acknowledgments


def acknowledge(engine: Any, tasks: list[dict]) -> None:
    for task in tasks:
        engine.state.execute(
            "DELETE FROM artifact_outbox WHERE kb_id=? AND folder=? AND revision=?",
            (engine.config.knowledge_base_id, task["folder"], task["revision"]),
        )
