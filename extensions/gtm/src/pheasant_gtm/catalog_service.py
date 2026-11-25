"""Deterministic reads over indexed GTM catalog publications."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, model_validator

from pheasant.ingestion.content_types import MEMORY_ARTIFACT_TYPE
from pheasant.security.authorization import require_permission
from pheasant.services import ServiceContext
from pheasant.services.errors import InvalidRequest, SourceNotFound
from pheasant.services.inventory import ARTIFACT_SCOPE, reader
from pheasant_gtm import catalog_store as gtm_catalog
from pheasant_gtm import collection_service as gtm_collections
from pheasant_gtm import enrichment
from pheasant_gtm.catalog import Enrichment, Key, Model, Scalar
from pheasant_gtm.naics import titles
from pheasant_gtm.plugin import PROVENANCE, gtm_settings
from pheasant_gtm.tech_taxonomy import canonical_category, category_products
from pheasant_gtm.technology import canonical_name

_PUBLICATIONS = (
    " FROM gtm_catalog_publications p JOIN artifacts a ON a.source_id=p.source_id "
    "AND a.relative_path=p.company_id||'/company-context.md' AND a.sha256=p.revision "
    f"WHERE p.source_id=? AND {ARTIFACT_SCOPE}"
)
_PROFILE = "CAST(p.profile_json AS jsonb)"


class CatalogQuery(Model):
    company_id: gtm_collections.CompanyId | None = None
    segment: gtm_collections.Segment | None = None
    industry_code: str | None = Field(default=None, max_length=5)
    naics_code: str | None = Field(default=None, max_length=6)
    technology: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        description="Named product, product alias, or technology category such as ERP or WMS.",
    )
    field: Key | None = None
    status: (
        Literal[
            "observed",
            "derived",
            "inferred",
            "proposed",
            "conflict",
            "missing",
            "blocked",
            "not_applicable",
        ]
        | None
    ) = None
    value: Scalar | None = None
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def filter_needs_field(self) -> CatalogQuery:
        if self.field is None and (self.status is not None or "value" in self.model_fields_set):
            raise ValueError("status and value filters require a field key")
        if self.industry_code and (
            self.industry_code not in titles()
            or len(self.industry_code) not in {2, 5}
            or (len(self.industry_code) == 5 and "-" not in self.industry_code)
        ):
            raise ValueError("industry_code must be a 2022 NAICS sector")
        if self.naics_code and self.naics_code not in titles():
            raise ValueError("Unknown 2022 NAICS code")
        return self


def _assert_catalog(context: ServiceContext, source: str, knowledge_base: str | None) -> str:
    kb, row = gtm_collections._source(context, source, knowledge_base)
    if context.state.source_removed(source) or not gtm_settings(json.loads(row["config_json"])).get(
        "catalog"
    ):
        raise SourceNotFound(source)
    return kb


def query(
    context: ServiceContext, source: str, criteria: CatalogQuery, knowledge_base: str | None = None
) -> dict:
    require_permission("knowledge.use")
    kb = _assert_catalog(context, source, knowledge_base)
    sql = [
        f"SELECT p.company_id,p.revision,a.relative_path,a.last_indexed_at,a.acl,"
        f"{_PROFILE}->>'company' AS company,{_PROFILE}->>'domain' AS domain,"
        f"{_PROFILE}->>'segment' AS segment,{_PROFILE}->'classification' AS classification",
        _PUBLICATIONS,
    ]
    params = [source, MEMORY_ARTIFACT_TYPE]
    if criteria.company_id is not None:
        sql.append("AND p.company_id=?")
        params.append(criteria.company_id)
    if criteria.segment is not None:
        sql.append(f"AND {_PROFILE}->>'segment'=?")
        params.append(criteria.segment)
    for key, code in (("sector_code", criteria.industry_code), ("code", criteria.naics_code)):
        if code is not None:
            sql.append(
                f"AND jsonb_extract_path_text({_PROFILE}, 'classification', CAST(? AS text))=?"
            )
            params.extend((key, code))
    if criteria.technology:
        category = canonical_category(criteria.technology)
        stack = f"{_PROFILE}#>'{{fields,technology_stack,value}}'"
        if category:
            names = category_products(category)
            sql.append(
                f"AND (jsonb_exists_any(jsonb_path_query_array({_PROFILE},'$.technology[*].name'),"
                f" CAST(? AS text[])) OR jsonb_exists_any({stack}, CAST(? AS text[])) OR "
                f"jsonb_exists_any({_PROFILE}#>'{{fields,web_technologies,value}}', "
                "CAST(? AS text[])))"
            )
            params.extend((names, names, names))
        else:
            name = canonical_name(criteria.technology)
            sql.append(
                f"AND ({_PROFILE}->'technology' @> CAST(? AS jsonb) OR {stack} @> CAST(? AS jsonb))"
            )
            params.extend((json.dumps([{"name": name}]), json.dumps([name])))
    if criteria.field is not None:
        field = f"jsonb_extract_path({_PROFILE},'fields',CAST(? AS text))"
        sql.append(f"AND {field} IS NOT NULL")
        params.append(criteria.field)
        if criteria.status is not None:
            sql.append(f"AND {field}->>'status'=?")
            params.extend((criteria.field, criteria.status))
        if "value" in criteria.model_fields_set:
            value = json.dumps(criteria.value)
            sql.append(
                f"AND ({field}->'value'=CAST(? AS jsonb) OR "
                f"{field}->'value' @> jsonb_build_array(CAST(? AS jsonb)))"
            )
            params.extend((criteria.field, value, criteria.field, value))
    sql.append('ORDER BY p.company_id COLLATE "C"')
    admit = reader(context, None, None)
    if admit is None:
        sql.append("LIMIT ? OFFSET ?")
        params.extend((criteria.limit + 1, criteria.offset))
    rows = context.state.rows(" ".join(sql), tuple(params))
    if admit is not None:
        rows = admit(rows)[criteria.offset : criteria.offset + criteria.limit + 1]
    return {
        "knowledge_base": kb,
        "source_name": source,
        "offset": criteria.offset,
        "has_more": len(rows) > criteria.limit,
        "companies": [
            {key: value for key, value in row.items() if key != "acl"}
            for row in rows[: criteria.limit]
        ],
    }


def _indexed(
    context: ServiceContext,
    source: str,
    company: str,
    knowledge_base: str | None,
    *,
    include_evidence: bool = False,
) -> tuple[str, dict]:
    kb = _assert_catalog(context, source, knowledge_base)
    columns = "p.profile_json,p.markdown,p.revision,a.acl,a.id,a.relative_path,a.last_indexed_at"
    if include_evidence:
        columns += ",p.evidence_json"
    rows = context.state.rows(
        "SELECT " + columns + _PUBLICATIONS + " AND p.company_id=?",
        (source, MEMORY_ARTIFACT_TYPE, company),
    )
    admit = reader(context, None, None)
    if admit is not None:
        rows = admit(rows)
    if not rows:
        raise SourceNotFound(f"{source}/{company}")
    return kb, rows[0]


def company(
    context: ServiceContext,
    source: str,
    company_id: str,
    *,
    field: str | None = None,
    knowledge_base: str | None = None,
) -> dict:
    require_permission("knowledge.use")
    kb, row = _indexed(context, source, company_id, knowledge_base)
    profile = json.loads(row["profile_json"])
    if field is not None:
        if field not in profile["fields"]:
            raise InvalidRequest(f"Unknown GTM field: {field}")
        selected = profile["fields"][field]
        profile["fields"] = {field: selected}
        profile.pop("technology", None)
        profile.pop("hubspot", None)
        profile.pop("offerings", None)
        profile["evidence"] = [
            entry for entry in profile["evidence"] if entry["id"] in selected["evidence_ids"]
        ]
    return {
        **profile,
        "knowledge_base": kb,
        "source_name": source,
        "company_id": company_id,
        "revision": row["revision"],
        "publication_pending": gtm_catalog.needs_publication(
            context.state, source, company_id, indexed_revision=row["revision"]
        ),
        "artifact_id": row["id"],
        "path": row["relative_path"],
        "indexed_at": row["last_indexed_at"],
        "markdown": row["markdown"] if field is None else None,
        "evidence": [
            {**entry, "graph_node_id": PROVENANCE.node_id(row["id"], entry["id"])}
            for entry in profile["evidence"]
        ],
    }


def evidence(
    context: ServiceContext,
    source: str,
    company_id: str,
    *,
    field: str | None = None,
    revision: str | None = None,
) -> dict:
    require_permission("sources.manage")
    _, row = _indexed(context, source, company_id, None, include_evidence=True)
    if revision is not None and revision != row["revision"]:
        raise InvalidRequest(
            "The company was reindexed. Open the evidence for the latest revision."
        )
    saved = json.loads(row["evidence_json"])
    links = saved.get("source_links", saved)
    result = {
        "source_name": source,
        "company_id": company_id,
        "revision": row["revision"],
        "source_links": links,
    }
    if field is None:
        return result
    indexed_fields = json.loads(row["profile_json"])["fields"]
    if field not in indexed_fields:
        raise InvalidRequest(f"Unknown GTM field: {field}")
    return {
        **result,
        "source_links": {
            key: links[key] for key in indexed_fields[field]["evidence_ids"] if key in links
        },
        "field": field,
        "research": saved.get("field_results", {}).get(field),
        "enrichment_traces": saved.get("enrichment_traces", {}).get(field, []),
        **(
            {"offering_traces": saved.get("offering_traces", {})}
            if field in {"products_services", "support_services", "customer_segments"}
            else {}
        ),
    }


def research_documents(context: ServiceContext, source: str, company_id: str) -> dict:
    """List the admitted documents in the last completed private research snapshot."""
    require_permission("sources.manage")
    _indexed(context, source, company_id, None)
    rows = context.state.rows(
        "SELECT state_json::jsonb#>'{snapshot,profile,source_outcomes}' AS documents, "
        "state_json::jsonb#>'{snapshot,profile,research,passages}' AS passage_count "
        "FROM gtm_catalog_companies WHERE source_id=? AND company_id=?",
        (source, company_id),
    )
    snapshot = rows[0] if rows else {}
    documents = [
        {key: row.get(key) for key in ("url", "title", "kind", "published")}
        for row in snapshot.get("documents") or ()
        if row["attributed"] and not row["quarantined"]
    ]
    return {
        "documents": sorted(
            documents, key=lambda row: (row["kind"], (row["title"] or "").casefold(), row["url"])
        ),
        "passage_count": snapshot.get("passage_count"),
    }


def refresh(
    context: ServiceContext,
    source: str,
    company_id: str,
    knowledge_base: str | None = None,
    *,
    deep: bool = False,
    product_page_limit: int | None = None,
    descriptions: Enrichment | None = None,
) -> dict:
    require_permission("sources.manage")
    _assert_catalog(context, source, knowledge_base)
    if product_page_limit is not None and (deep or not 1 <= product_page_limit <= 60):
        raise InvalidRequest("Choose a product-only budget of 1 to 60 or a deep refresh")
    if descriptions is not None:
        enrichment.validate_model(context.config, context.state, descriptions)
    if not gtm_catalog.request_refresh(
        context.state,
        source,
        company_id,
        deep=deep,
        product_page_limit=product_page_limit,
        enrichment=descriptions.model_dump(mode="json") if descriptions is not None else None,
    ):
        raise SourceNotFound(f"{source}/{company_id}")
    return {**gtm_collections.refresh(context, source, knowledge_base), "company_id": company_id}
