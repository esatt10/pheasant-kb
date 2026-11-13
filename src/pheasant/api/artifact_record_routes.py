"""Typed artifact adapters; legacy /memory routes are deprecated aliases."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from pydantic import BaseModel

from pheasant.services import artifact_records as records


class RecordBody(BaseModel):
    text: str
    scope: str = "user"
    subject: str | None = None
    supersedes: str | None = None
    tags: list[str] = []
    sync: bool = True
    kind: str = "fact"
    principal: str | None = None
    valid_until: str | None = None


def register_record_routes(app: FastAPI, services: Any) -> None:
    from pheasant.services import job_history, retrieval_configurations

    @app.middleware("http")
    async def deprecate_memory(request: Request, call_next: Any):
        response = await call_next(request)
        if request.url.path == "/memory" or request.url.path.startswith("/memory/"):
            response.headers["Deprecation"] = "true"
            suffix = request.url.path.removeprefix("/memory")
            successor = (
                "/artifacts/records" + suffix
                if suffix in ("", "/enable")
                else "/artifacts" + suffix
            )
            response.headers["Link"] = f'<{successor}>; rel="successor-version"'
        return response

    @app.delete("/evaluation/runs/{run_id}")
    def delete_evaluation_run(run_id: str) -> dict:
        return job_history.delete_evaluation(services, run_id)

    @app.post("/tuning/baselines/{name}")
    def save_retrieval_baseline(name: str) -> dict:
        return retrieval_configurations.save_base(services, name)

    @app.get("/artifacts/publications/{artifact_id}/versions/{revision_id}")
    def get_artifact_version(
        artifact_id: str, revision_id: str, principal: str | None = None
    ) -> dict:
        from pheasant.artifacts.publications import version
        from pheasant.services.errors import ArtifactNotFound

        found = version(services.state, services.config.knowledge_base_id, artifact_id, revision_id)
        if found is None:
            raise ArtifactNotFound(artifact_id)
        from pheasant.security.identity import effective_principal

        writer = found.get("written_by") or found.get("owner")
        if (
            found.get("scope", (found.get("payload") or {}).get("scope", "org")) != "org"
            and writer
            and writer != effective_principal(principal)
        ):
            raise ArtifactNotFound(artifact_id)
        return {"artifact_id": artifact_id, "revision_id": revision_id, "content": found}

    @app.post("/artifacts/records")
    def write_artifact_record(body: RecordBody) -> dict:
        return records.write(services, records.WriteRequest(**body.model_dump()))

    @app.get("/artifacts/records")
    def list_artifact_records(
        scope: str | None = None, current_only: bool = True, principal: str | None = None
    ) -> dict:
        return records.list_records(services, principal, scope=scope, current_only=current_only)

    @app.post("/artifacts/records/enable")
    def enable_artifact_records() -> dict:
        found = records.source(services, create=True)
        return {"status": "enabled", "source": found.model_dump(mode="json")}

    @app.get("/artifacts/records/{artifact_id}")
    def get_artifact_record(artifact_id: str, principal: str | None = None) -> dict:
        return records.get_record(services, artifact_id, principal)
