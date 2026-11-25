# GTM company research

Pheasant builds a company profile from public web evidence and prepares a HubSpot company-property preview. A run takes a company name, an official website, or a name and several URLs. The IDE or HTTP client can drive a checkpointed workflow. A tool-free model can optionally review selected passages.

GTM is an installable Pheasant extension. Its source, API, UI, PostgreSQL migrations and parser live in [`extensions/gtm`](../../extensions/gtm). The [extension protocol](../extensions.md) describes installation and discovery.

## Managed GTM collections

A managed collection is a regular Pheasant source with type `gtm_research`. Each sync rechecks the source's public URLs and writes a searchable `company-context.md` at a stable document ID. Incremental sync reuses the sandbox output for unchanged page bytes and skips indexing when the curated Markdown has the same content hash. A full sync re-parses the upstream material. The standard sync job, scheduler, source filters, and graph index apply.

Enable the optional sidecar with the root Compose stack. Use your usual deployment overrides alongside this file.

```sh
docker compose -f compose.yaml -f deploy/compose/docker-compose.gtm.yml up -d --build
```

The overlay builds the Pheasant image with the GTM extension and runs `gtm-parser` without a network, credentials, region state, or exposed ports. The API and indexer reach it through a shared Unix socket. Each request still executes in a seccomp-confined parser. The indexer fetches public URLs and controls publication. Standalone regions use the local parser sandbox described below. Set `BRAVE_SEARCH_API_KEY` if the API or indexer should discover targets or supporting sources through Brave.

Open **GTM** in Pheasant's main navigation to browse company catalogs. Use **Add company** to register a company and its seed URLs. In **Sources**, choose **Taxonomy** on a catalog row to browse its company folders in the workspace. The active jobs tray shows how many page attempts the indexer has checkpointed.

Register programmatically with `POST /gtm/collections` or `register_gtm_collection` over Pheasant's existing streamable HTTP MCP endpoint at `/mcp/`. Pass the same object as the MCP tool's `request`, plus its `knowledge_base` argument.

```json
{
  "name": "acme-context",
  "target": {"company": "Acme Systems", "website": "https://acme.example", "search": false},
  "input_source": "acme-web",
  "sync_now": true,
  "refresh_interval_seconds": 86400
}
```

`input_source` is optional and points to an existing, enabled web collection. A GTM sync takes that collection's current URL list along with any inline `target.urls`, and fetches the public upstream pages directly. Update the upstream list with `edit_source_urls`, then resync the GTM source to apply it. Managed sources default to a daily schedule. Configure or pause it through the normal sync-pipeline controls.

| Operation | HTTP | MCP |
| --- | --- | --- |
| Register or update a collection | `POST /gtm/collections` | `register_gtm_collection` |
| Inspect collection coverage and revisions | `GET /gtm/collections/{source_name}` | `describe_gtm_collection` |
| Trigger an incremental refresh | `POST /gtm/collections/{source_name}/sync` | `sync_gtm_collection` |
| Download full evidence for an analyst | `GET /gtm/collections/{source_name}/evidence` | |

When a refresh is queued, follow `wait_for_sync` or `GET /sources/{source_name}/wait` using the previous `last_indexed_at`. Then verify the new indexed revision and query `search_context` with `source_name` set to the collection name. During a refresh, `describe_gtm_collection` reports the crawler phase and page attempts. Interrupted refreshes retain their private checkpoint. An unresolved identity or exhausted transient fetch failure leaves the previous indexed revision available.

The curated company document contains code-defined technology, business activity, and capability terms, operator-configured enum values, selected firmographics reported by official sources, corroborated numeric or Boolean facts, and opaque source references. Browse or search publisher text separately under the source's `pages/` directory. Exact field extracts and model proposals are available in the analyst evidence workflow.

## Group companies under a single source

Set `company_id` when registering a managed target to create or reuse a company catalog. The catalog is one source, and each stable company ID gets its own directory. The **Companies** action in Sources opens the catalog in **GTM**, and **Taxonomy** opens its company folders.

```json
{
  "name": "gtm-companies",
  "company_id": "acme",
  "target": {"company": "Acme", "website": "https://acme.example", "naics_code": "513210", "search": false},
  "sync_now": true
}
```

The source publishes `acme/company-context.md`. Use the same source name and a different company ID to add another directory. Register the same company ID to update its target or field definitions. Single-company sources keep their existing names and paths. A catalog's company targets and checkpoints live in the region's PostgreSQL backup.

GTM shows the NAICS sector as **Industry** and the selected more specific classification as **Sub-industry**. The code and titles come from the offline 2022 U.S. Census structure. Set `target.naics_code` when registering an account. Otherwise Pheasant uses a valid code explicitly reported by an attributable source. Accounts remain unclassified when evidence is missing or conflicting. An operator-assigned code represents a sales classification. Multi-industry companies can be classified at sector level until the account scope warrants more precision. Existing API clients can still query their legacy `segment` labels. [U.S. Census Bureau NAICS reference](https://www.census.gov/naics/)

The catalog sync checks up to 20 due companies per pass and lists every prepared document for the normal indexer. Due companies are checked daily by default. The source can pick up batches every 15 minutes when the scheduler is enabled. A per-company refresh requests an earlier check, and a failed refresh retains the prior indexed profile and graph evidence.

| Operation | HTTP | MCP |
| --- | --- | --- |
| Add or update a company | `POST /gtm/collections` with `company_id` | `register_gtm_collection` |
| Query indexed companies | `POST /gtm/catalogs/{source_name}/query` | `query_gtm_catalog` |
| List NAICS codes and labels | `GET /gtm/naics` | |
| Discover technology categories | `GET /gtm/technology-taxonomy` | `get_gtm_technology_taxonomy` |
| Read a company or a named field | `GET /gtm/catalogs/{source_name}/companies/{company_id}` with optional `field` | `get_gtm_company` |
| Refresh one company | `POST /gtm/catalogs/{source_name}/companies/{company_id}/sync` | `sync_gtm_company` |
| Download publisher URLs or inspect one field's research evidence | `GET /gtm/catalogs/{source_name}/companies/{company_id}/evidence` with optional `field` and `revision` | |

Query with `{ "field": "technology_stack", "status": "observed", "value": "Kubernetes" }` for an exact, typed match. Scalars compare exactly and a value can match an element of a multi-value field. Query results use company ID order with `limit` and `offset`. A company response includes its indexed revision, Markdown, field definitions, typed values, statuses, and graph evidence IDs. Missing and withheld values return `null` with their status and a separate `withheld` flag.

Open **About us and field evidence** in the company brief to see how a field was populated. The panel shows the indexed value and status, field definition, and graph evidence. Users with `sources.manage` can see the extraction method, publisher wording, source links and excerpts, alternatives, and the reason a field is missing or blocked. That evidence belongs to the indexed revision. Refresh a company published before field-level evidence was added. Publisher wording stays in the authorized evidence response and is displayed as plain text.

**What they do** combines supported first-party statements about a company's operations, products, or purpose. Each part of the explanation cites the archived pages that support it. Field responses expose this as `detail.summary` and `detail.statements` alongside the typed value used for filters. Where a field resolves multiple items, `detail.items` can explain the evidence for each value, including whether a technology appeared in hiring, implementation, internal use, or a product integration. Unresolved fields retain their status and reason. Matching passages are available in the authorized evidence panel for review. The report refresh can rebuild these details from the last completed archive before the next crawl.

### Product and service research

The **Products and services** section builds an inventory from attributed first-party archives. It distinguishes company offerings from implementation partner services and attaches citations to each supported capability, buyer segment, delivery model, and support term. Product and support values also appear in the corresponding typed fields. An authorized analyst can read the literal supporting passages through the field evidence endpoint. A publisher's product mention, a recruiting page, or a keyword in an article title cannot establish that the company sells an offering.

Choose **Research offerings** on an existing company to start from its verified archive and reserve 48 additional fetch attempts for offering pages. The crawler prioritizes product and service catalogs, offering descriptions, documentation, pricing, and support pages that it has discovered on the verified site. Marketplace listings remain archived when the wider crawl finds them. They can establish a company offering when they explicitly identify its provider or manufacturer. The crawl uses the same parser isolation, fetch rules, and checkpoints as the rest of GTM. The report records the starting and ending offering counts, attempted pages, and whether the frontier or budget ran out.

### Grounded company descriptions

GTM first extracts the company's structured facts and then enriches its human-readable descriptions by default. Auto prefers an available GPT-6 Luna deployment in Pheasant's shared model catalog, then tries the region's default and other selectable chat models. An explicit catalog ID uses that model. If the selected model is unavailable, or a claim fails validation, the extracted profile is still published. Verified descriptions and their references appear in the company brief, field details, HTTP and MCP output, and archived company-context Markdown.

Set `target.enrichment` at registration through HTTP or MCP. In the UI, use **Enrich descriptions with a model**. For an existing catalog company, open **Description settings** and choose **Update descriptions**. The corresponding HTTP operation is `POST /gtm/catalogs/{source_name}/companies/{company_id}/sync?enrichment_enabled=true&enrichment_model=auto`. With MCP, call `sync_gtm_company` with `enrichment: {"enabled": true, "model": "auto"}`. A description-only update reuses the existing archive when available. Disable it with `enabled: false` or `enrichment_enabled=false`.

Built-in long text covers the company overview, mission and activities, offerings, customers, support, technology, initiatives, and qualified service hypotheses. Declare `rich_text: true` on a custom text field to include it. The model receives bounded passages per field and exact supporting quotes are checked before a separate claim verification pass. Validated prose keeps a citation per statement. Typed facts, filters, field eligibility, and CRM promotion continue to use deterministic evidence rules. The profile reports the model and provider-reported token counts. Pheasant reuses verified results for matching evidence and model selection.

**Sales briefing** shows documented customer segments, dated changes, and questions for a first call. The company brief can recover explicit offerings and buyer facts from pages already in the archive. The briefing cites the announcement date and labels discovery questions as questions. Use a planned change as a reason to qualify the account and verify its current status during discovery.

Set `target.product_page_limit` from 0 through 60 to reserve a separate budget on future company syncs. It defaults to 0. Use `target.product_urls` to seed official offering pages explicitly. For a catalog company with a saved archive, use `POST /gtm/catalogs/{source_name}/companies/{company_id}/sync?product_page_limit=48` or call `sync_gtm_company` with `product_page_limit`. The selected budget is saved for future refreshes of that company. A new company first needs the usual identity and broad collection.

In **Sources → Taxonomy**, open a company directory, then `pages/`, to browse the verified publisher documents alongside the curated `company-context.md`. Pheasant indexes the crawler's screened text in its normal search, document and chunk pipeline. For a captured HTML response, the **Web page** view displays the saved HTML beside its indexed text in a sandboxed frame. Documents obtained from feeds or job-board data can have indexed text without an HTML snapshot. The **Research documents** panel and `GET /gtm/catalogs/{source_name}/companies/{company_id}/research-documents` also show titles and publisher links from the last completed private research snapshot and require `sources.manage`.

Combine `{ "industry_code": "51", "naics_code": "513210", "technology": "K8s" }` to find companies by sector, exact sub-industry code, and normalized technology name. The account brief separates internal use, changes in progress, hiring, product and integration signals, mentions, and detections in the public site's markup. Each technology relationship shows the sources that support it. Undated first-party technical pages are labeled undated and can establish product or technology coverage. Sales hypotheses retain their separate evidence requirements.

The GTM page, HTTP API and MCP catalog query also accept technology categories such as `ERP`, `WMS`, `WES`, `TMS`, `OMS`, `MES`, `QMS`, `SCADA`, `PLM`, `EHR`, `POS` and `EAM`. For example, `{ "industry_code": "48-49", "technology": "WMS" }` filters transportation and warehousing companies with evidence of at least one named warehouse management platform. Open the company to see whether the source describes its own use, a hiring requirement, or an offering. HubSpot's Web Technologies preview uses evidence from public website markup.

Use **Browse technology categories** in GTM to discover categories, including artificial intelligence, machine learning, generative AI, MLOps, optimization and operations research, and business process automation. Filters accept stable IDs, display labels and aliases. A broader filter also matches products in its narrower categories. Named technologies require evidence and retain their relationship to the company. Published Markdown includes the applicable category names for normal Pheasant search and embedding. Retrieve the versioned vocabulary as JSON-LD with `GET /gtm/technology-taxonomy?format=skos` or `get_gtm_technology_taxonomy` with `format: skos`. The vocabulary uses [W3C SKOS](https://www.w3.org/TR/skos-reference/).

The **HubSpot property review** groups suggested Company properties. Industry and Industry group require a match to the portal's options. Store the exact NAICS code in a custom property. Web Technologies uses public-site detections and needs a match to HubSpot's fixed choices. Hiring evidence, integrations, and internal platform mentions belong in account research. The review makes no HubSpot writes. [HubSpot default company properties](https://knowledge.hubspot.com/properties/hubspot-crm-default-company-properties)

The graph links the curated document to indexed pages with `derived_from` edges. Each edge indicates whether a field cites the page and carries the relevant field keys. Follow `has_chunk` from the page to its searchable passages. The page ID is stable across content updates, while superseded chunk IDs are removed. Evidence summary nodes also remain available with the publisher domain, source kind, date where known, and field keys.

### Try a 20-company catalog

The [demo manifest](../../examples/gtm-research/demo-companies.json) spans AI, ERP, technology, logistics, manufacturing, and consulting and includes operator-assigned NAICS codes. Its 12-page and six-posting budgets expand the technical and hiring coverage. Set `PHEASANT_API_TOKEN` for your local region and run the seeder from the repository root.

```sh
python examples/gtm-research/register_demo.py --base-url http://127.0.0.1:8765 --export indexed-context.json
```

Use `--manifest` for your own company list. The command registers companies, advances due batches, waits for the normal indexing pipeline, and verifies the revision against the curated Markdown before exporting. The bundle contains the taxonomy, indexed company documents, typed fields and definitions, and graph evidence IDs. Re-run the same command after an interruption to reuse registered targets and checkpoints. Use `--register-only` to stage the targets, or `--no-wait` to queue a single incremental sync and return immediately. The region's normal indexing settings govern embeddings.

Open **GTM** and select `gtm-20-company-demo`. Use **View syncs** to follow the job and select a company to inspect fields and graph evidence. The demo uses bounded public collection and permits partial coverage after identity verification. Set `target.enrichment.enabled` to `false` for a model-free run.

Six demo targets include verified alternatives such as an investor site, newsroom, developer portal, or careers site. GTM keeps the primary website first and shows when a fallback established the identity. When the verified publisher is on a different host, the brief displays that publisher and withholds the primary domain and website fields from HubSpot review.

## Start and monitor a run

Use an installed checkout and its existing PostgreSQL region. Install GTM with `python -m pip install -e ./extensions/gtm`. The operator CLI resolves the usual Pheasant config and database.

A Linux process uses the built-in seccomp parser sandbox. Pheasant's Debian-based image includes `libseccomp.so.2`. For a non-Linux CLI or API process, build the local worker image with Linux containers enabled in Docker.

```sh
docker build -f extensions/gtm/Dockerfile.parser -t pheasant-gtm-sandbox:local .
```

`PHEASANT_GTM_SANDBOX_IMAGE` optionally selects a trusted locally built worker image. The CLI uses the same sandbox for every company. If confinement cannot start, the step fails and preserves its checkpoint for retry.

```sh
pheasant gtm start "Acme Systems" --website acme.example
pheasant gtm start https://acme.example --no-search --run
pheasant gtm start "Acme Systems" --website acme.example --url https://acme.example/careers --url https://acme.example/news --fields examples/gtm-research/fields.yaml
pheasant gtm status RUN_ID
pheasant gtm resume RUN_ID
pheasant gtm report RUN_ID --evidence --output profile.json
```

`--config` goes immediately after `gtm` when using a nondefault config. `start` saves the run ID before fetching any pages. `resume` prints progress to stderr and the final structured summary to stdout. `step` advances one operation and `report` reads the summary without advancing it. If the process exits, resume with the same ID. A hard crash can leave a lease for up to 150 seconds. A worker that has lost its lease cannot overwrite a later checkpoint. Checkpoints created before isolation are held with status `security_upgrade_required`. Start a new run to re-collect those sources.

Set `BRAVE_SEARCH_API_KEY` in the CLI or API process for unattended name discovery and external news, careers, company fact, and SEC filing searches. An explicit domain works with direct site discovery. When a name is ambiguous, an analyst can inspect the candidate domains in the evidence export and start a new run with the intended domain.

## Isolation and evidence handling

- **Parser.** The fetcher checks public destinations and robots rules. A separate process reads HTML, XML, search responses and job board payloads through bounded pipes. It gets an empty environment on Linux and seccomp denies filesystem opens, sockets and process creation. The runtime limits CPU, memory and elapsed time. The non-Linux launcher also runs the worker in a network-disabled, read-only container with no host mounts or supplied credentials. [Linux seccomp](https://docs.kernel.org/userspace-api/seccomp_filter.html).
- **Publisher data.** The parser removes hidden markup and quarantines documents with recognizable prompt-injection patterns in visible or structured data. Quarantine keeps only metadata and blocks extraction, link discovery, indexing and model review from that document. Treat admitted publisher text as untrusted when retrieving it. Identical document hashes and substantial copied quotations count as one origin for corroboration.
- **Tool use and trust.** Standalone model and IDE observations stay proposals. Managed description enrichment uses tool-free calls over selected public passages and publishes only source-checked prose with references. Model descriptions cannot satisfy dependencies, schedule work, or promote values into CRM properties. Single-origin company facts beyond the core identity fields go to the HubSpot review section.
- **Human review.** Full evidence is an explicit export containing publisher-controlled text and URLs. Keep it out of tool-enabled agent context. These controls constrain the effects of an injection or poisoned source. A publisher can still make a plausible false claim, so high-impact fields require corroboration and human review. [OWASP prompt-injection guidance](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html).

## Research and evidence policy

### Deep company research

Choose **Deep research** when registering a source or **Enable deep research and refresh** on an existing company brief. The action updates that company's crawl settings for subsequent resyncs. Set `depth: "deep"` in an HTTP or MCP research target, or pass `--depth deep` on the CLI. Deep mode defaults to 80 page attempts and 36 job postings. Explicit budgets can lower those limits or allow up to 120 page attempts.

Deep mode gathers a bounded primary corpus, indexes normalized passages in a company-scoped lexical index, and runs field-specific searches. It applies each field's source, recency, and dependency rules. It reserves up to 16 page attempts to follow up on evidence gaps, including corporate facts, customer support, investor disclosures, and technology investment. When external search is enabled with a Brave key, it can add up to three first-party searches during this follow-up. Otherwise it prioritizes URLs already discovered on the company site. The job checkpoints the fetched documents and reports document, passage, field coverage, and search-attempt counts.

The 20-company demo uses deep mode with 64 collection page attempts, up to 24 job postings, and 24 additional targeted offering-page attempts per company. These are limits. A company's crawl can finish earlier when it runs out of relevant pages, and a page attempt can end without indexable content.

Deep research can collect literal company overviews and mission statements from first-party corporate pages. The field evidence panel shows additional matching source passages even when the available evidence does not resolve a value. Source managers can read those excerpts and quotations. Pheasant search and MCP continue to return the curated facts, counts, and graph references.

Description, About us, and Technology use up to 16 matching passages and favor distinct pages. Description covers the core business and specific offerings. About us adds customers, footprint, and operating context when documented. Technology explains the role of named systems in products, integrations, hiring, or company operations. Model enrichment aims for four to seven distinct, cited claims per field when the evidence supports them. Sparse evidence can still yield a short value or a deterministic fallback.

For an individual run, add `--review` or `review: true` to use Pheasant's configured chat model on selected evidence. Deep mode requests low reasoning where supported by the shared provider and restricts review to eight tool-free batches of at most five fields. Model observations remain proposals. Managed profiles use the separate description enrichment policy.

| Stage | Method |
| --- | --- |
| Resolve identity | Compare the supplied website and search candidates with organization markup and exact title segments. If the homepage cannot establish identity, try a supplied website path and then operator-supplied official fallback URLs. Stop on equally ranked search candidates. |
| Find primary sources | Follow relevant first-party links, RSS or Atom links, and sitemaps. Prioritize company information, investor pages, jobs, news and support. Search results supply URLs to verify. |
| Read hiring evidence | Parse `JobPosting` structured data and use Greenhouse, Lever, and Ashby public feeds linked from the employer site. For an oversized Greenhouse feed, read board metadata and fetch selected postings within budget. Preserve posting dates and expiry where available. Undated live board entries use the observation date for recency checks, with publication left empty. |
| Read company updates | Use structured article dates, newsrooms, blogs and independent reporting. Require entity clues before using third-party evidence. Keep undated items labeled. |
| Resolve fields | Apply source, age, type, corroboration and dependency rules. Preserve conflicting candidates and their citations. Use quotations and access dates in the report. |
| Interpret signals | Keep the hiring or published context with technologies and initiatives. Report potential services needs as hypotheses with triggering evidence. |
| Prepare CRM preview | Suggest supported company properties. HubSpot enum values, currency-bearing figures, and agent proposals need mapping or review before use. Account-specific lifecycle, owner, deal and activity data comes from HubSpot. |

By default a run budgets 36 collection page attempts and 24 job documents, plus identity checks and up to six grouped searches. It tracks page failures, transient retries, progress, attribution and skipped sources in PostgreSQL. Adjust `--page-limit`, `--job-limit` and `--signal-days` for scope. The reader respects robots.txt, public address checks, redirect limits and response size limits. It reads rendered server HTML, structured JSON and XML feeds. Client-rendered, paywalled, and PDF-only sources may need another accessible publisher URL. SEC discovery is limited to accessible filing pages, so financial fields can remain missing on companies with little machine-readable disclosure.

Sources for the research method and adapters. [Organization markup](https://developers.google.com/search/docs/appearance/structured-data/organization), [job posting markup](https://developers.google.com/search/docs/appearance/structured-data/job-posting), [Greenhouse](https://docs.greenhouse.io/job-board.html), [Lever](https://github.com/lever/postings-api), [Ashby](https://developers.ashbyhq.com/docs/public-job-posting-api), [Brave Search](https://api-dashboard.search.brave.com/documentation/services/web-search).

## Adjust collection while a run is active

The crawl reads public HTML, follows relevant same-site links, and checks sitemaps and feeds. The sandbox also reads structured job records from inert JSON embedded in a page and from publisher-linked JSON endpoints. It follows same-site pagination and public frames associated with careers pages. Recognized ATS adapters use publisher-linked public boards and can step from a large board summary to selected job details. A JavaScript shell without usable data is recorded as a coverage gap. The parser never executes publisher scripts.

The `acquisition` object accepts the following settings on an individual run or the `target` of a managed source.

| Setting | Default | Effect |
| --- | --- | --- |
| `follow_links` | `true` | Expand related pages, feeds, and sitemaps. Set to `false` for a seed-driven crawl. |
| `embedded_data` | `true` | Parse inert application JSON and follow supported data links. |
| `site_adapters` | `true` | Use the recognized ATS board adapters. |
| `job_board_urls` | `[]` | Prioritize public Greenhouse, Lever, or Ashby boards whose employer attribution the operator has confirmed. The run verifies the company's own website before querying these boards and records the operator-supplied provenance. |
| `identity_fallback_urls` | `[]` | Try up to five operator-confirmed official URLs in order after the primary site cannot establish identity. Requires the company name and primary website. Each alternative follows the same public URL, robots, redirect and parser checks, and the first verified source establishes the profile's domain. Add these in the GTM registration form or send them under `acquisition`. |
| `max_attempts` | `2` | Bound attempts per transiently failing URL to one through four. |
| `retry_delay_seconds` | `0` | Add a minimum delay between transient retries. Publisher `Retry-After` takes precedence if it is longer. |
| `allow_partial` | `false` | Let a managed sync publish partial coverage after a transient failure if it verified the company. The default retains the last published revision. Individual runs always expose their partial results. |

Post to `/gtm/runs/{run_id}/control`, call `control_gtm_run`, or run `pheasant gtm control RUN_ID update.yaml`. A control update can set `paused`, patch `acquisition`, adjust `page_limit`, add `add_urls`, or requeue `retry_urls` with transient failures and attempts remaining. Apply acquisition changes before collection finishes. A pause takes effect between checkpoints. When `next_step_at` is present, use that Unix timestamp to schedule the next step. The run can use other hosts while one is cooling down. Cooldowns longer than two minutes leave a recorded coverage gap instead of holding a worker. Access restrictions and robots rules continue to apply to every requested URL.

## Inspect stored results and token usage

Use `/gtm/runs/{run_id}/persistence`, `inspect_gtm_persistence`, or `pheasant gtm persistence RUN_ID` for checkpoint bytes, parsed document counts, candidate counts, acquisition outcomes, and token usage. The evidence download includes the saved normalized documents and per-URL task state. A managed collection's evidence download also includes the exact `company-context.md` and the snapshot size. The state store keeps normalized parser output and, for collection resyncs, a reusable parser cache. It does not keep original response bodies.

Deterministic acquisition and extraction use zero model tokens. Hosted review and Decisions usage is recorded separately by stage, model, input, output, cached input and reasoning tokens when the provider reports them. A `null` means the total is unknown. Cached input and reasoning counts are breakdowns and should not simply be added to the input and output totals. IDE-agent tokens and optional indexing-embedding usage are metered outside the GTM run. Each hosted review batch requests up to 2,200 reply tokens within the selected model's limits. Reasoning models can use Pheasant's shared reasoning allowance. Actual usage depends on the selected passages and the provider response.

## Add fields and dependencies

Pass a YAML or JSON array as `--fields`, or use the HTTP request's `fields` array. Each entry needs a `key` and a `definition`. A custom key adds a field. A default key replaces that field's complete specification for the run. The effective catalog is stored at creation.

- `type` supports `text`, `integer`, `number`, `boolean`, `url`, `date`, and `enum`. For an enum, list allowed `options`.
- `labels` enables deterministic extraction from a publisher's literal `Label: value` rows. `keywords` focuses discovery and review. Definition and key terms guide review when explicit keywords are absent.
- `sources` restricts evidence to `official`, `jobs`, `news`, `filing`, or `external`. `max_age_days` sets a freshness window and `min_origins` requires corroboration across publisher origins.
- `applies_when` lists typed `field` and `equals` pairs. Each condition must pass. A missing or conflicting dependency blocks the field. A resolved dependency with a different value makes the field inapplicable. The default ticker field requires positive public-company status.
- `derive_from` with ordered `bands` derives a single enum from a resolved integer. The default employee range uses this policy. Dependency cycles are rejected before a run starts.
- `multiple` returns distinct values and their evidence. `inference` allows hypotheses with source quotes and required context. Use `hubspot` for a verified internal property name where a CRM projection is appropriate.

See [custom field examples](../../examples/gtm-research/fields.yaml). The catalog is based on [HubSpot's default company properties](https://knowledge.hubspot.com/properties/hubspot-crm-default-company-properties). Check actual internal names, allowed enum values, and writable properties with the [HubSpot Properties API](https://developers.hubspot.com/docs/api-reference/legacy/crm/properties/guide) before using the preview for import.

## IDE and hosted model review

The [IDE skill](../../examples/gtm-research/gtm-research/SKILL.md) orchestrates the CLI through its structured responses. For analyst review, export `pheasant gtm review-packet RUN_ID --evidence --output review.json`. Submit observations with `apply-review`. Each object has `field`, `value`, `passage_id`, `quote`, and optional `context`. The quote must appear in the selected passage and factual values must be present in the text. The tool records accepted, rejected, and duplicate counts.

Enable automatic review with `--review` or `review: true`. GTM uses Pheasant's default chat model and shared credential resolution, including region connections and Foundry managed identity. Choose another catalog ID with `--model` or `model` where `models.selectable` permits it. The same model list is available from the Models API and the `list_models` MCP tool. Review sends selected public passages through a bounded, tool-free completion.

Pass `--review --decisions` or set both flags in the request to use the region's Decisions model. Choose another permitted catalog ID with `--decision-model` or `decision_model`. The region's `assistant.decisions.mode` governs whether the classifier can reorder passages. Field admissibility stays in code, and deterministic ranking remains available when the classifier is unavailable.

## HTTP workflow

Use the existing Pheasant API bearer token. Contributor or Admin permissions allow start, step, evidence download, and review submission. Requests are scoped to their authenticated principal and region. The local operator CLI can inspect region runs by ID.

| Method and path | Result |
| --- | --- |
| `POST /gtm/runs` | Creates a run and returns its ID. Body accepts `company`, `website`, `urls`, `fields`, the run flags, and optional shared model IDs. |
| `GET /gtm/runs/{run_id}` | Current phase, lease, document counts, progress and latest issue stage. |
| `POST /gtm/runs/{run_id}/step` | Advances one operation. A concurrent worker receives HTTP 409. An unavailable sandbox returns HTTP 503 and preserves the checkpoint. |
| `POST /gtm/runs/{run_id}/control` | Pauses or resumes a run and changes collection settings or URLs. |
| `GET /gtm/runs/{run_id}/persistence` | Returns storage counts, acquisition outcomes, and measured provider token usage. |
| `GET /gtm/runs/{run_id}/report` | Structured summary for clients that can take actions. |
| `GET /gtm/runs/{run_id}/evidence` | Full profile, URL tasks, and stored parser output as a JSON attachment containing untrusted publisher text. |
| `GET /gtm/runs/{run_id}/review` | Opaque passage and source IDs for a completed run. |
| `POST /gtm/runs/{run_id}/review` | Validates reviewed observations and saves them as proposals. |

A calling workflow starts once, stores the run ID, calls `step` until the phase is `complete`, and reads the report. Step calls can be resumed after failures. A completed report can contain missing fields or unresolved identity. `page_limit`, `job_limit`, `signal_days`, `search`, `review`, and `decisions` have the same semantics as the CLI. The endpoints preview data for HubSpot and perform no CRM writes.

The corresponding MCP tools for individual runs are `start_gtm_run`, `step_gtm_run`, `control_gtm_run`, `get_gtm_run`, `inspect_gtm_persistence`, and `get_gtm_report`.
