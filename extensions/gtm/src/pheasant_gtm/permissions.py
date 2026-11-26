"""GTM privileges on the shared HTTP and MCP surfaces."""

HTTP = {
    ("GET", "/gtm/collections/{source_name}"): "knowledge.use",
    ("GET", "/gtm/catalogs/{source_name}/companies/{company_id}"): "knowledge.use",
    ("GET", "/gtm/naics"): "knowledge.use",
    ("GET", "/gtm/technology-taxonomy"): "knowledge.use",
    ("GET", "/gtm/catalogs/{source_name}/companies/{company_id}/evidence"): "sources.manage",
    ("GET", "/gtm/catalogs/{source_name}/companies/{company_id}/research-documents"): (
        "sources.manage"
    ),
    ("GET", "/gtm/collections/{source_name}/evidence"): "sources.manage",
    ("GET", "/gtm/runs/{run_id}"): "knowledge.use",
    ("GET", "/gtm/runs/{run_id}/evidence"): "sources.manage",
    ("GET", "/gtm/runs/{run_id}/persistence"): "knowledge.use",
    ("GET", "/gtm/runs/{run_id}/report"): "knowledge.use",
    ("GET", "/gtm/runs/{run_id}/review"): "knowledge.use",
    ("POST", "/gtm/collections"): "sources.manage",
    ("POST", "/gtm/catalogs/{source_name}/query"): "knowledge.use",
    ("POST", "/gtm/catalogs/{source_name}/companies/{company_id}/sync"): "sources.manage",
    ("POST", "/gtm/collections/{source_name}/sync"): "sources.manage",
    ("POST", "/gtm/runs"): "sources.manage",
    ("POST", "/gtm/runs/{run_id}/step"): "sources.manage",
    ("POST", "/gtm/runs/{run_id}/control"): "sources.manage",
    ("POST", "/gtm/runs/{run_id}/review"): "sources.manage",
}

MCP = {
    "register_gtm_collection": "sources.manage",
    "query_gtm_catalog": "knowledge.use",
    "get_gtm_technology_taxonomy": "knowledge.use",
    "get_gtm_company": "knowledge.use",
    "sync_gtm_company": "sources.manage",
    "describe_gtm_collection": "knowledge.use",
    "sync_gtm_collection": "sources.manage",
    "start_gtm_run": "sources.manage",
    "step_gtm_run": "sources.manage",
    "control_gtm_run": "sources.manage",
    "inspect_gtm_persistence": "knowledge.use",
    "get_gtm_run": "knowledge.use",
    "get_gtm_report": "knowledge.use",
}
