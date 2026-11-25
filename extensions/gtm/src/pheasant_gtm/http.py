"""HTTP entry points for the checkpointed GTM company research workflow."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from pheasant_gtm import catalog_service as gtm_catalog
from pheasant_gtm import collection_service as gtm_collections
from pheasant_gtm import run_service as gtm
from pheasant_gtm.catalog import (
    Enrichment,
    ExcerptLimit,
    PageLimit,
    ProductPageLimit,
    Request,
    ReviewRequest,
    RunControl,
    Term,
)
from pheasant_gtm.collection_service import CollectionRequest
from pheasant_gtm.naics import catalog as naics_reference
from pheasant_gtm.tech_taxonomy import taxonomy


def _attachment(packet: dict) -> Response:
    return Response(
        json.dumps(packet, ensure_ascii=True),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="gtm-evidence.json"',
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


def register_gtm_routes(app: FastAPI, *, services: Any) -> None:
    def call(operation, *args, **kwargs):
        return gtm.operation(services, None, operation, *args, **kwargs)

    @app.get("/gtm/naics")
    def naics_codes() -> dict:
        return naics_reference()

    @app.get("/gtm/technology-taxonomy")
    def technology_taxonomy(format: Literal["json", "skos"] = "json") -> Response:
        return JSONResponse(
            taxonomy(skos=format == "skos"),
            media_type="application/ld+json" if format == "skos" else "application/json",
        )

    @app.post("/gtm/collections")
    def register_collection(request: CollectionRequest) -> dict:
        return gtm_collections.register(services, request)

    @app.get("/gtm/collections/{source_name}")
    def describe_collection(source_name: str) -> dict:
        return gtm_collections.describe(services, source_name)

    @app.post("/gtm/collections/{source_name}/sync")
    def refresh_collection(source_name: str) -> dict:
        return gtm_collections.refresh(services, source_name)

    @app.get("/gtm/collections/{source_name}/evidence")
    def collection_evidence(source_name: str) -> Response:
        return _attachment(gtm_collections.evidence(services, source_name))

    @app.post("/gtm/catalogs/{source_name}/query")
    def query_catalog(source_name: str, criteria: gtm_catalog.CatalogQuery) -> dict:
        return gtm_catalog.query(services, source_name, criteria)

    @app.get("/gtm/catalogs/{source_name}/companies/{company_id}")
    def catalog_company(source_name: str, company_id: str, field: str | None = None) -> dict:
        return gtm_catalog.company(services, source_name, company_id, field=field)

    @app.get("/gtm/catalogs/{source_name}/companies/{company_id}/evidence")
    def catalog_evidence(
        source_name: str, company_id: str, field: str | None = None, revision: str | None = None
    ) -> Response:
        return _attachment(
            gtm_catalog.evidence(services, source_name, company_id, field=field, revision=revision)
        )

    @app.get("/gtm/catalogs/{source_name}/companies/{company_id}/research-documents")
    def catalog_research_documents(source_name: str, company_id: str) -> Response:
        return JSONResponse(
            gtm_catalog.research_documents(services, source_name, company_id),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/gtm/catalogs/{source_name}/companies/{company_id}/sync")
    def refresh_company(
        source_name: str,
        company_id: str,
        deep: bool = False,
        product_page_limit: ProductPageLimit | None = None,
        page_limit: PageLimit | None = None,
        excerpt_limit: ExcerptLimit | None = None,
        enrichment_enabled: bool | None = None,
        enrichment_model: Term | None = None,
    ) -> dict:
        descriptions = None
        if enrichment_enabled is not None or enrichment_model is not None:
            descriptions = Enrichment(
                enabled=enrichment_enabled is not False, model=enrichment_model or "auto"
            )
        return gtm_catalog.refresh(
            services,
            source_name,
            company_id,
            deep=deep,
            product_page_limit=product_page_limit,
            page_limit=page_limit,
            excerpt_limit=excerpt_limit,
            descriptions=descriptions,
        )

    @app.post("/gtm/runs", status_code=201)
    def create(request: Request) -> dict:
        return call(gtm.start, request, config=services.config)

    @app.get("/gtm/runs/{run_id}")
    def status(run_id: str) -> dict:
        return call(gtm.status, run_id)

    @app.post("/gtm/runs/{run_id}/step")
    def step(run_id: str) -> dict:
        return call(gtm.step, run_id, config=services.config)

    @app.post("/gtm/runs/{run_id}/control")
    def control(run_id: str, update: RunControl) -> dict:
        return call(gtm.control, run_id, update)

    @app.get("/gtm/runs/{run_id}/persistence")
    def persistence(run_id: str) -> dict:
        return call(gtm.persistence, run_id)

    @app.get("/gtm/runs/{run_id}/report")
    def report(run_id: str) -> dict:
        return call(gtm.report, run_id)

    @app.get("/gtm/runs/{run_id}/evidence")
    def evidence(run_id: str) -> Response:
        return _attachment(call(gtm.report, run_id, evidence=True))

    @app.get("/gtm/runs/{run_id}/review")
    def packet(run_id: str) -> dict:
        return call(gtm.review_packet, run_id)

    @app.post("/gtm/runs/{run_id}/review")
    def review(run_id: str, request: ReviewRequest) -> dict:
        return call(gtm.apply_review, run_id, request.model_dump(mode="json")["observations"])
