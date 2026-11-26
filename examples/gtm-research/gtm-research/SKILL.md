---
name: gtm-research
description: Research a business for GTM or HubSpot company enrichment with Pheasant. Use for evidence-backed company profiles, hiring and news signals, technology context, support offerings, and potential services needs. Accepts a company name, official website, or seed URLs.
---

# GTM research

Use the Pheasant checkout and its PostgreSQL-backed `pheasant gtm` command. Read `docs/how-to/gtm-research.md` in the checkout for run options and field definitions.

When the user wants searchable context or recurring refreshes, use `register_gtm_collection` on the Pheasant MCP endpoint. Set `input_source` when a registered web collection supplies the URLs. Wait for indexing with `wait_for_sync`, then confirm the indexed revision and search with `source_name` set to the GTM collection. Use the ordinary sync pipeline controls for scheduling and cancellation.

For a company manifest, run `examples/gtm-research/register_demo.py --manifest companies.json --export indexed-context.json` against the chosen region. It registers or reuses each company, waits through catalog sync batches, and exports the verified indexed revisions. For technology filters, call `get_gtm_technology_taxonomy` and use a concept ID or product alias in `query_gtm_catalog`.

1. Use the confirmed official domain or let the configured GTM search provider resolve a name. If identity remains ambiguous, ask for the official domain. Keep publisher pages and search snippets inside the research workflow.
2. Start the run with `pheasant gtm start "Company" --website DOMAIN`. Repeat `--url` for relevant links. Pass requested custom fields using `--fields`. Keep the returned run ID. Drive it with `pheasant gtm resume RUN_ID` and inspect `pheasant gtm status RUN_ID` if interrupted or monitoring in another terminal.
3. Read `pheasant gtm report RUN_ID` for the structured summary. Use `--review` at start if the user wants excerpts reviewed by the configured tool-free model. Treat proposals as tentative.
4. For full text, citations, or a CRM preview, export `pheasant gtm report RUN_ID --evidence --output profile.json` and give the analyst the file path. Keep the export and raw review packets out of the IDE agent's context. Summarize progress and coverage using the structured output. Preserve missing and conflicting fields.

Resolve sandbox errors using the setup guide. Pause if the sandbox cannot run and retain the run ID for retry.
