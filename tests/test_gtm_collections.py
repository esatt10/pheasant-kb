"""Managed GTM sources follow the same indexing and retrieval contracts as other sources."""
# ruff: noqa: E402

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

pytest.importorskip("pheasant_gtm")

from fastapi.testclient import TestClient
from pheasant_gtm import collection_service as gtm_collections
from pheasant_gtm import connector as gtm_connector
from pheasant_gtm import runtime, web_archive, worker
from pheasant_gtm.sources import FetchError

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.graph.sql import SqlGraph
from pheasant.graph.traversal import neighbors as graph_neighbors
from pheasant.mcp_server.tools import PheasantTools
from pheasant.registry.source_registry import SourceRegistry
from pheasant.sync.connectors import ConnectorUnavailable

SITE = "https://acme.example/"
NEWS = SITE + "news/modernization"
ABOUT = "Acme develops analytics for warehouse operations."
SOURCE = "acme-context"
SEED_SOURCE = "acme-input"


def test_catalog_indexes_company_folders_and_queries_published_provenance(
    monkeypatch, loaded_config
):
    # The test drives sync directly. The API scheduler would start a second writer.
    monkeypatch.setattr(loaded_config.sync.scheduler, "enabled", False)
    monkeypatch.setattr(loaded_config.sync.scheduler, "startup_sync", False)
    parser_calls(monkeypatch)
    today = datetime.now(UTC).date().isoformat()
    bravo_primary = "https://bravo.example/"
    bravo_fallback = "https://investors.bravo.example/"
    calls = []
    research_updates = []
    list_items = gtm_connector.GTMConnector.list_items

    def track_catalog_progress(connector):
        if connector.catalog:
            connector.listing_progress = lambda *args: research_updates.append(args)
        return list_items(connector)

    def fetch(target, _robots):
        calls.append(target)
        if target == bravo_primary:
            raise FetchError("network read failed", retryable=True)
        if target == NEWS:
            body = (
                '<title>Acme modernization</title><script type="application/ld+json">'
                f'{{"@type":"NewsArticle","datePublished":"{today}"}}'
                "</script><p>We are implementing a cloud migration using Azure.</p>"
                "<p>Publisher-only sentence.</p>"
            )
            return target, "text/html", body
        company = "Acme" if target == SITE else "Bravo"
        return (
            target,
            "text/html",
            (
                f"<title>{company}</title><p>{company} provides analytics.</p>"
                '<script src="https://www.googletagmanager.com/gtm.js?id=example"></script>'
                "<p>We run PostgreSQL and Kubernetes in production.</p>"
                + (
                    "<p>We use Infor WMS, SAP ECC, UiPath and IBM ILOG CPLEX.</p>"
                    if company == "Bravo"
                    else f'<meta name="description" content="{ABOUT}">'
                    "<p>We run PyTorch and MLflow in production.</p>"
                )
            ),
        )

    monkeypatch.setattr(gtm_connector, "fetch", fetch)
    monkeypatch.setattr(gtm_connector.GTMConnector, "list_items", track_catalog_progress)
    tools = PheasantTools(loaded_config)
    registry = SourceRegistry(loaded_config, tools.state)
    app = create_app(loaded_config)
    source = "company-research"
    prefix = f"/gtm/catalogs/{source}"
    definition = {"key": "support_tier", "definition": "The verified customer support tier."}
    with TestClient(app, base_url="http://localhost:8765") as client:

        def register(company, website, urls, fields, segment=None, fallbacks=(), naics_code=None):
            reply = client.post(
                "/gtm/collections",
                json={
                    "name": source,
                    "company_id": company.lower(),
                    "segment": segment,
                    "sync_now": False,
                    "target": {
                        "company": company,
                        "naics_code": naics_code,
                        "website": website,
                        "urls": urls,
                        "fields": fields,
                        "search": False,
                        "acquisition": {
                            "follow_links": False,
                            "identity_fallback_urls": list(fallbacks),
                            "max_attempts": 1,
                        },
                    },
                },
            )
            assert reply.status_code == 200, reply.text
            return reply.json()

        assert (
            register("Acme", SITE, [NEWS], [definition], "technology", naics_code="513210")["path"]
            == "acme/company-context.md"
        )
        register("Bravo", bravo_primary, [], [], fallbacks=[bravo_fallback])
        assert sum(row["name"] == source for row in registry.list_sources()) == 1
        assert client.post(prefix + "/query", json={}).json()["companies"] == []
        registry.adopt(source)
        assert tools.engine.sync_source(source, "incremental").indexed_artifacts == 5
        completed = client.get(f"/sources/{source}/wait", params={"timeout_seconds": 0}).json()
        assert completed["state"] == "finished"
        assert set(completed["last_sync"]["gtm_catalog"]["attempted_company_ids"]) == {
            "acme",
            "bravo",
        }
        repeated = register("Acme", SITE, [NEWS], [definition], "technology", naics_code="513210")
        assert repeated["company_pending"] is False
        concepts = client.get("/gtm/technology-taxonomy").json()["concepts"]
        assert any(item["id"] == "optimization" for item in concepts)
        skos = client.get("/gtm/technology-taxonomy", params={"format": "skos"})
        assert skos.headers["content-type"].startswith("application/ld+json")
        assert skos.json()["@graph"][0]["@type"] == "skos:ConceptScheme"
        assert ("researching", 0, 2, "acme · 1 page attempt saved") in research_updates
        assert ("researching", 1, 2, "bravo · 1 page attempt saved") in research_updates
        listing = client.post(prefix + "/query", json={"limit": 1}).json()
        assert [row["company_id"] for row in listing["companies"]] == ["acme"]
        assert listing["has_more"]
        page_two = client.post(prefix + "/query", json={"offset": 1}).json()
        assert [row["company_id"] for row in page_two["companies"]] == ["bravo"]
        bravo = client.get(prefix + "/companies/bravo").json()
        assert bravo["identity_method"] == "official_fallback"
        assert bravo["domain"] == "investors.bravo.example"
        assert "operator-supplied official fallback" in bravo["markdown"]
        for technology in (
            "Infor Warehouse Management",
            "WMS",
            "warehouse management (wms)",
            "ERP",
            "Automation",
            "optimization",
        ):
            response = client.post(prefix + "/query", json={"technology": technology})
            assert response.status_code == 200, response.text
            assert [item["company_id"] for item in response.json()["companies"]] == ["bravo"]
        assert client.post(prefix + "/query", json={"technology": "TMS"}).json()["companies"] == []
        for technology in ("AI", "machine-learning", "MLOps"):
            response = client.post(prefix + "/query", json={"technology": technology})
            assert [item["company_id"] for item in response.json()["companies"]] == ["acme"]
        assert "Infor WMS" not in bravo["fields"]["web_technologies"]["value"]
        matches = client.post(
            prefix + "/query", json={"field": "technology_stack", "value": "Azure"}
        )
        assert matches.status_code == 200, matches.text
        assert [row["company_id"] for row in matches.json()["companies"]] == ["acme"]
        lead_matches = client.post(
            prefix + "/query",
            json={"industry_code": "51", "naics_code": "513210", "technology": "Microsoft Azure"},
        )
        assert lead_matches.status_code == 200, lead_matches.text
        (company_row,) = lead_matches.json()["companies"]
        assert company_row["classification"]["sub_industry"] == "Software Publishers"
        in_segment = client.post(
            prefix + "/query", json={"segment": "technology", "field": "domain"}
        )
        assert in_segment.status_code == 200, in_segment.text
        assert [(row["company_id"], row["segment"]) for row in in_segment.json()["companies"]] == [
            ("acme", "technology")
        ]
        tree = client.get("/documents/tree", params={"source_name": source}).json()
        assert "acme" in json.dumps(tree) and "bravo" in json.dumps(tree)
        acme = client.get(prefix + "/companies/acme").json()
        assert acme["segment"] == "technology"
        assert (
            acme["fields"]["support_tier"]["definition"]["definition"] == definition["definition"]
        )
        assert set(acme["fields"]["technology_stack"]["value"]) == {
            "Azure",
            "PostgreSQL",
            "Kubernetes",
            "PyTorch",
            "MLflow",
        }
        assert "## Technology categories" in acme["markdown"]
        assert "- Machine learning" in acme["markdown"]
        assert acme["classification"]["source"] == "operator"
        assert acme["fields"]["naics_code"]["value"] == "513210"
        technologies = {row["name"]: row for row in acme["technology"]}
        assert technologies["PostgreSQL"]["signals"][0]["relationship"] == "internal_use"
        assert technologies["Azure"]["signals"][0]["relationship"] == "change_in_progress"
        assert technologies["Google Tag Manager"]["signals"][0]["relationship"] == "public_website"
        assert {row["property"] for row in acme["hubspot"]} >= {
            "Industry",
            "Industry group",
            "Web Technologies",
        }
        assert "Publisher-only" not in json.dumps(acme)
        detail = client.get(prefix + "/companies/acme", params={"field": "technology_stack"}).json()
        assert list(detail["fields"]) == ["technology_stack"]
        assert detail["evidence"] and set(detail["evidence"][0]["field_keys"]) >= {
            "technology_stack"
        }
        ref = next(item["graph_node_id"] for item in detail["evidence"] if item["kind"] == "news")
        graph = tools.engine.graph_builder.graph
        edges = graph.get_edge_data(acme["artifact_id"], ref)
        assert any(edge["type"] == "derived_from" for edge in edges.values())
        page_path = f"acme/pages/news-modernization-{web_archive.reference(NEWS)}.txt"
        params = {"source_name": source, "path": page_path}
        document = client.get("/documents/content", params=params).json()
        assert document["page"] == {"url": NEWS, "available": True}
        assert "Publisher-only sentence." in document["content"]
        assert "<script" not in document["content"]
        html = client.get("/documents/page", params=params)
        assert html.status_code == 200
        assert "sandbox" in html.headers["content-security-policy"] and "<script" in html.text
        page_id = document["document"]["id"]
        link = next(
            edge
            for edge in graph.get_edge_data(acme["artifact_id"], page_id).values()
            if edge.get("extension_input")
        )
        assert link["type"] == "derived_from" and "technology_stack" in link["field_keys"]
        chunk_ids = {
            target
            for _, target, edges in graph.out_edges(page_id)
            if any(edge["type"] == "has_chunk" for edge in edges.values())
        }
        assert chunk_ids
        downloaded = client.get(prefix + "/companies/acme/evidence")
        assert "attachment" in downloaded.headers["content-disposition"]
        assert NEWS in downloaded.json()["source_links"].values()
        assert acme["fields"]["about"]["status"] == "observed"
        assert acme["fields"]["about"]["value"] is None and acme["fields"]["about"]["withheld"]
        assert ABOUT not in json.dumps(acme)
        about_response = client.get(
            prefix + "/companies/acme/evidence",
            params={"field": "about", "revision": acme["revision"]},
        )
        assert about_response.status_code == 200, about_response.text
        assert about_response.headers["cache-control"] == "no-store"
        about = about_response.json()
        assert list(about["source_links"].values()) == [SITE]
        assert about["research"]["value"] == ABOUT and about["research"]["method"] == "metadata"
        assert about["research"]["evidence"][0]["quote"] == ABOUT
        assert about["research"]["evidence"][0]["url"] == SITE
        missing = client.get(prefix + "/companies/acme/evidence", params={"field": "description"})
        assert (
            missing.json()["research"]["reason"] == "No attributable evidence met the field policy"
        )
        assert (
            client.get(
                prefix + "/companies/acme/evidence", params={"field": "about", "revision": "stale"}
            ).status_code
            == 422
        )
        headers = {"accept": "application/json, text/event-stream"}
        hello = client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "gtm-catalog-test", "version": "1"},
                },
            },
        )
        assert hello.status_code == 200, hello.text
        mcp = client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_gtm_company",
                    "arguments": {
                        "knowledge_base": loaded_config.knowledge_base_id,
                        "source_name": source,
                        "company_id": "acme",
                        "field": "technology_stack",
                    },
                },
            },
        )
        assert mcp.status_code == 200, mcp.text
        result = mcp.json()["result"]
        assert not result.get("isError") and "Azure" in json.dumps(result)
        tools.engine.sync_source(source, "full")
        assert client.get(prefix + "/companies/acme").json()["revision"] == acme["revision"]
        previous_fetch_count = len(calls)
        assert tools.engine.sync_source(source, "incremental").indexed_artifacts == 0
        assert len(calls) == previous_fetch_count
        neighbors = graph_neighbors(
            SqlGraph(tools.state, loaded_config.knowledge_base_id),
            acme["artifact_id"],
            2,
            ["derived_from", "has_chunk"],
        )
        assert page_id in json.dumps(neighbors)
        assert any(chunk_id in json.dumps(neighbors) for chunk_id in chunk_ids)

        register(
            "Acme", SITE, [], [definition | {"definition": "The published customer support tier."}]
        )
        assert client.get(prefix + "/companies/acme").json()["revision"] == acme["revision"]
        assert tools.engine.sync_source(source, "incremental").indexed_artifacts == 1
        current = client.get(prefix + "/companies/acme").json()
        assert (
            current["artifact_id"] == acme["artifact_id"]
            and current["revision"] != acme["revision"]
        )
        assert "segment" not in current
        assert ref not in graph
        assert client.get("/documents/page", params=params).status_code == 404
        assert page_id not in graph
        assert (
            client.post(
                prefix + "/query", json={"field": "technology_stack", "value": "Azure"}
            ).json()["companies"]
            == []
        )
        from pheasant_gtm import catalog_store as gtm_catalog

        assert gtm_catalog.request_refresh(tools.state, source, "bravo", deep=True)
        previous = client.get(prefix + "/companies/bravo").json()
        assert previous["revision"] == bravo["revision"] and previous["publication_pending"]
        assert tools.engine.sync_source(source, "incremental").indexed_artifacts == 1
        deep = client.get(prefix + "/companies/bravo").json()
        assert not deep["publication_pending"]
        assert deep["research"]["documents"] == 1
        assert deep["research"]["fields_searched"] > 0
        inventory = client.get(prefix + "/companies/bravo/research-documents")
        assert inventory.status_code == 200 and inventory.headers["cache-control"] == "no-store"
        assert inventory.json()["passage_count"] == deep["research"]["passages"]
        assert [item["url"] for item in inventory.json()["documents"]] == [bravo_fallback]
        assert set(inventory.json()["documents"][0]) == {"url", "title", "kind", "published"}
        assert "We use Infor WMS" not in json.dumps(deep)
        response = client.get(
            prefix + "/companies/bravo/evidence",
            params={"field": "technology_stack", "revision": deep["revision"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["research"]["items"]
        assert tools.engine.sync_source(source, "incremental").indexed_artifacts == 0


def parser_calls(monkeypatch):
    calls = []

    def parse(operation, payload):
        calls.append((operation, payload.get("target")))
        return worker._dispatch({"operation": operation, **payload})

    monkeypatch.setattr(runtime, "_parse", parse)
    return calls


def test_collection_is_a_resyncable_search_source(monkeypatch, loaded_config):
    technology = "Azure"
    outage = False
    fetched = []
    parsed = parser_calls(monkeypatch)
    today = datetime.now(UTC).date().isoformat()

    def fetch(target, _robots):
        fetched.append(target)
        if target == SITE:
            body = (
                '<title>Acme</title><script type="application/ld+json">'
                '{"@type":"Organization","name":"Acme","url":"https://acme.example/"}'
                "</script><p>Publisher-only sentence.</p>"
            )
            return SITE, "text/html", body
        if target == NEWS:
            if outage:
                raise FetchError("HTTP 503", retryable=True)
            body = (
                '<title>Acme news</title><script type="application/ld+json">'
                f'{{"@type":"NewsArticle","datePublished":"{today}"}}'
                f"</script><p>We are implementing a cloud migration using {technology}.</p>"
            )
            return NEWS, "text/html", body
        return target, "application/xml", "<urlset/>"

    monkeypatch.setattr(gtm_connector, "fetch", fetch)
    tools = PheasantTools(loaded_config)
    kb = loaded_config.knowledge_base_id
    registry = SourceRegistry(loaded_config, tools.state)
    registry.install(
        {
            "name": SEED_SOURCE,
            "type": "web_collection",
            "path": "/unused",
            "urls": [NEWS],
        }
    )
    app = create_app(loaded_config)
    with TestClient(app) as client:
        assert {"id": "gtm", "ui_routes": ["/gtm"]} in client.get("/extensions").json()[
            "extensions"
        ]
        created = client.post(
            "/gtm/collections",
            json={
                "name": SOURCE,
                "target": {"company": "Acme", "website": SITE, "search": False},
                "input_source": SEED_SOURCE,
                "sync_now": False,
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["source"]["type"] == "gtm_research"
    registry.adopt(SOURCE)
    progress = []
    first_sync = tools.engine.sync_source(
        SOURCE, "incremental", on_progress=lambda *event: progress.append(event)
    )
    assert first_sync.indexed_artifacts == 3
    assert any(event[0] == "researching" and event[1] > 0 for event in progress)
    first = gtm_collections.describe(tools.services, SOURCE)
    assert first["indexed_revision"] == first["curated_revision"]
    result = tools.search_context(kb, "Azure", source_name=SOURCE, mode="text")
    assert "company-context.md" in json.dumps(result)
    publisher = tools.search_context(kb, "Publisher-only sentence", source_name=SOURCE, mode="text")
    assert "pages/home-" in json.dumps(publisher)
    initial_parses = len(parsed)
    assert tools.engine.sync_source(SOURCE, "incremental").indexed_artifacts == 0
    assert len(parsed) == initial_parses
    assert fetched.count(NEWS) == 2

    outage = True
    with pytest.raises(ConnectorUnavailable, match="unresolved transient"):
        tools.engine.sync_source(SOURCE, "incremental")
    assert (
        gtm_collections.describe(tools.services, SOURCE)["indexed_revision"]
        == first["indexed_revision"]
    )
    outage = False
    technology = "Terraform"
    assert tools.engine.sync_source(SOURCE, "incremental").indexed_artifacts == 2
    changed = gtm_collections.describe(tools.services, SOURCE)
    assert changed["indexed_revision"] != first["indexed_revision"]
    current = tools.search_context(kb, "Terraform", source_name=SOURCE, mode="text")
    assert "company-context.md" in json.dumps(current)
    assert "Azure" not in json.dumps(current)

    registry.install(
        {
            "name": SEED_SOURCE,
            "type": "web_collection",
            "path": "/unused",
            "urls": [],
        }
    )
    assert tools.engine.sync_source(SOURCE, "incremental").indexed_artifacts == 1
    removed = tools.search_context(kb, "Terraform", source_name=SOURCE, mode="text")
    assert "company-context.md" not in json.dumps(removed)


def test_mcp_registers_the_same_source(loaded_config):
    kb = loaded_config.knowledge_base_id
    headers = {"accept": "application/json, text/event-stream"}
    app = create_app(loaded_config)
    with TestClient(app, base_url="http://localhost:8765") as client:
        hello = client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "gtm-test", "version": "1"},
                },
            },
        )
        assert hello.status_code == 200, hello.text
        reply = client.post(
            "/mcp/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "register_gtm_collection",
                    "arguments": {
                        "knowledge_base": kb,
                        "request": {
                            "name": SOURCE,
                            "target": {"website": SITE, "search": False},
                            "sync_now": False,
                        },
                    },
                },
            },
        )
        assert reply.status_code == 200, reply.text
        result = reply.json()["result"]
        assert not result.get("isError"), result
        status = client.get(f"/gtm/collections/{SOURCE}")
        assert status.status_code == 200, status.text
        assert status.json()["source_type"] == "gtm_research"
        assert status.json()["indexed_revision"] is None


def test_managed_source_rejects_model_review(loaded_config: PheasantConfig):
    with TestClient(create_app(loaded_config)) as client:
        response = client.post(
            "/gtm/collections",
            json={
                "name": SOURCE,
                "target": {"website": SITE, "review": True},
                "sync_now": False,
            },
        )
    assert response.status_code == 422
