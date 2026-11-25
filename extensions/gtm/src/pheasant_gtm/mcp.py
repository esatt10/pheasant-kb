"""MCP adapters for first-class GTM sync sources and research runs."""

from __future__ import annotations

from typing import Any, Literal

from pheasant_gtm import catalog_service as gtm_catalog
from pheasant_gtm import collection_service as gtm_collections
from pheasant_gtm import run_service as gtm
from pheasant_gtm.catalog import (
    Enrichment,
    ExcerptLimit,
    PageLimit,
    ProductPageLimit,
    Request,
    RunControl,
)
from pheasant_gtm.collection_service import CollectionRequest
from pheasant_gtm.tech_taxonomy import taxonomy


def register_gtm_tools(mcp: Any, tools: Any, anticipated: Any) -> None:
    @mcp.tool()
    @anticipated
    def register_gtm_collection(knowledge_base: str, request: CollectionRequest) -> dict:
        """Register a company as a managed GTM context source.

        Provide a source name and target (company, website, URLs or a
        combination). Set company_id to group profiles under one catalog source.
        Optionally reference an existing web collection by
        input_source. Confirm indexed content with wait_for_sync.
        """
        return gtm_collections.register(tools.services, request, knowledge_base)

    @mcp.tool()
    @anticipated
    def query_gtm_catalog(
        knowledge_base: str, source_name: str, criteria: gtm_catalog.CatalogQuery | None = None
    ) -> dict:
        """List indexed companies by industry, normalized technology, or exact field filters.

        Technology accepts named products, common aliases, or categories such as ERP and WMS.
        """
        return gtm_catalog.query(
            tools.services, source_name, criteria or gtm_catalog.CatalogQuery(), knowledge_base
        )

    @mcp.tool()
    @anticipated
    def get_gtm_technology_taxonomy(
        knowledge_base: str, format: Literal["json", "skos"] = "json"
    ) -> dict:
        """List stable category IDs, aliases and broader concepts, or export SKOS JSON-LD."""
        tools.services.knowledge_base(knowledge_base)
        return taxonomy(skos=format == "skos")

    @mcp.tool()
    @anticipated
    def get_gtm_company(
        knowledge_base: str, source_name: str, company_id: str, field: str | None = None
    ) -> dict:
        """Read indexed Markdown, typed fields and definitions, and evidence graph IDs."""
        return gtm_catalog.company(
            tools.services, source_name, company_id, field=field, knowledge_base=knowledge_base
        )

    @mcp.tool()
    @anticipated
    def sync_gtm_company(
        knowledge_base: str,
        source_name: str,
        company_id: str,
        deep: bool = False,
        product_page_limit: ProductPageLimit | None = None,
        enrichment: Enrichment | None = None,
        page_limit: PageLimit | None = None,
        excerpt_limit: ExcerptLimit | None = None,
    ) -> dict:
        """Refresh a company. Optionally set general page or company excerpt limits up to 250.

        An excerpt-only change can reuse saved pages.
        Set enrichment alone to reprocess descriptions.
        """
        return gtm_catalog.refresh(
            tools.services,
            source_name,
            company_id,
            knowledge_base,
            deep=deep,
            product_page_limit=product_page_limit,
            page_limit=page_limit,
            excerpt_limit=excerpt_limit,
            descriptions=enrichment,
        )

    @mcp.tool()
    @anticipated
    def describe_gtm_collection(knowledge_base: str, source_name: str) -> dict:
        """Read progress, coverage, and the curated and indexed revisions."""
        return gtm_collections.describe(tools.services, source_name, knowledge_base)

    @mcp.tool()
    @anticipated
    def sync_gtm_collection(knowledge_base: str, source_name: str) -> dict:
        """Start a regular incremental sync and refresh the company's public evidence."""
        return gtm_collections.refresh(tools.services, source_name, knowledge_base)

    @mcp.tool()
    @anticipated
    def start_gtm_run(knowledge_base: str, request: Request) -> dict:
        """Create a checkpoint for an individual company research run."""
        return gtm.operation(
            tools.services, knowledge_base, gtm.start, request, config=tools.services.config
        )

    @mcp.tool()
    @anticipated
    def step_gtm_run(knowledge_base: str, run_id: str) -> dict:
        """Advance one checkpoint. Honor next_step_at (Unix seconds) when a retry is delayed."""
        return gtm.operation(
            tools.services, knowledge_base, gtm.step, run_id, config=tools.services.config
        )

    @mcp.tool()
    @anticipated
    def control_gtm_run(knowledge_base: str, run_id: str, update: RunControl) -> dict:
        """Pause or resume a run and change its acquisition or pending URLs during collection."""
        return gtm.operation(tools.services, knowledge_base, gtm.control, run_id, update)

    @mcp.tool()
    @anticipated
    def inspect_gtm_persistence(knowledge_base: str, run_id: str) -> dict:
        """Inspect checkpoint size, record counts, acquisition methods and model token usage."""
        return gtm.operation(tools.services, knowledge_base, gtm.persistence, run_id)

    @mcp.tool()
    @anticipated
    def get_gtm_run(knowledge_base: str, run_id: str) -> dict:
        """Read progress for an individual research run."""
        return gtm.operation(tools.services, knowledge_base, gtm.status, run_id)

    @mcp.tool()
    @anticipated
    def get_gtm_report(knowledge_base: str, run_id: str) -> dict:
        """Read the structured report with opaque evidence references."""
        return gtm.operation(tools.services, knowledge_base, gtm.report, run_id)
