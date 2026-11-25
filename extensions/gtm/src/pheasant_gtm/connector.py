"""A managed company source, driven by the ordinary Pheasant sync engine."""

from __future__ import annotations

import json
import time
from functools import partial
from hashlib import sha256

from pheasant import cancel_scope
from pheasant.sync.connectors import (
    ConnectorHealth,
    ConnectorItem,
    ConnectorPayload,
    ConnectorUnavailable,
    SourceConnector,
)
from pheasant_gtm import catalog_store as gtm_catalog
from pheasant_gtm import collection_store as gtm_collections
from pheasant_gtm import field_report, presentation, runbook, web_archive, workflow
from pheasant_gtm.catalog import Request, ordered_fields
from pheasant_gtm.context import curated, field_results, markdown
from pheasant_gtm.plugin import SOURCE_TYPE
from pheasant_gtm.runtime import IsolationUnavailable, reuse_parsed
from pheasant_gtm.sources import FetchError, fetch

DOCUMENT = gtm_catalog.DOCUMENT


def effective_target(state, settings: dict, seeds: list[str]) -> Request:
    config = dict(settings.get("target") or {})
    claim = settings.get("input_source")
    if claim:
        row = state.get_source(claim)
        if not row or row["type"] != "web_collection" or not row["enabled"]:
            raise ValueError("GTM input source must be an enabled web collection")
        seeds = [*seeds, *json.loads(row["config_json"]).get("urls", [])]
    config["urls"] = list(dict.fromkeys(seeds))
    config["review"], config["decisions"] = False, False
    return Request.model_validate(config)


class GTMConnector(SourceConnector):
    connector_type = SOURCE_TYPE
    complete_listing = True

    @property
    def catalog(self) -> bool:
        return bool((self.source.extension_settings or {}).get("catalog"))

    def _target(self) -> Request:
        settings = self.source.extension_settings
        if settings is None:
            raise ConnectorUnavailable("Register a GTM target before syncing this source")
        try:
            return effective_target(self.state, settings, self.source.urls)
        except ValueError as exc:
            raise ConnectorUnavailable(
                "GTM collection target or linked URL list is invalid"
            ) from exc

    def list_items(self) -> list[ConnectorItem]:
        if self.catalog:
            return self._catalog_items()
        target = self._target()
        self._snapshot = self._collect(target)
        pages = self._page_items(self._snapshot["web_pages"])
        return [
            *pages,
            ConnectorItem(
                identity=f"gtm:{self.source.name}:{DOCUMENT}",
                relative_path=DOCUMENT,
                uri=target.website or f"gtm:{self.source.name}",
                mime_type="text/markdown",
                sha256=self._snapshot["revision"],
                size_bytes=len(self._snapshot["markdown"].encode("utf-8")),
            ),
        ]

    def _page_items(self, pages: dict, company: str | None = None) -> list[ConnectorItem]:
        return [
            item
            for item in web_archive.items(pages, self.source.name, company)
            if self._allows_relative_path(item.relative_path)
        ]

    def _catalog_items(self) -> list[ConnectorItem]:
        name = self.source.name
        republished = []
        if self.sync_mode == "incremental":
            for row in gtm_catalog.outdated_reports(self.state, name, field_report.VERSION):
                cancel_scope.check()
                settings = json.loads(row["settings_json"])
                try:
                    target = effective_target(self.state, settings, settings["urls"])
                except ValueError:
                    continue
                snapshot = json.loads(row["state_json"])["snapshot"]
                if (
                    snapshot.get("target_key")
                    != sha256(target.model_dump_json().encode()).hexdigest()
                ):
                    continue
                self._publish(row, settings, target, snapshot)
                republished.append(row["company_id"])
        due = gtm_catalog.due(
            self.state, name, time.time(), refresh_all=self.sync_mode in {"full", "repair"}
        )
        failed = []
        for index, row in enumerate(due, start=1):
            cancel_scope.check()
            company = row["company_id"]
            if self.listing_progress:
                self.listing_progress("researching", index - 1, len(due), company)
            settings = json.loads(row["settings_json"])
            try:
                target = effective_target(self.state, settings, settings["urls"])
                snapshot = self._collect(target, row=row, catalog_progress=(index - 1, len(due)))
            except (ValueError, ConnectorUnavailable) as exc:
                if isinstance(exc.__cause__, IsolationUnavailable):
                    raise
                # Keep the prior publication and allow other companies to proceed.
                gtm_catalog.checked(self.state, name, row, time.time(), failed=True)
                failed.append(company)
                continue
            self._publish(row, settings, target, snapshot)
            gtm_catalog.checked(self.state, name, row, time.time())
        self.listing_details = {
            "gtm_catalog": {
                "attempted": len(due),
                "attempted_company_ids": [row["company_id"] for row in due],
                "failed_company_ids": failed,
                "republished_company_ids": republished,
            }
        }
        inventories = gtm_catalog.page_inventory(self.state, name)
        items = []
        for row in gtm_catalog.listings(self.state, name):
            company = row["company_id"]
            items.extend(self._page_items(inventories.get(company, {}), company))
            if not self._allows_relative_path(gtm_catalog.path(company)):
                continue
            items.append(
                ConnectorItem(
                    identity=f"gtm:{name}:{company}",
                    relative_path=gtm_catalog.path(company),
                    uri=f"gtm:{name}/{company}",
                    mime_type="text/markdown",
                    sha256=row["revision"],
                    metadata={"company_id": company},
                )
            )
        return items

    def _publish(self, row: dict, settings: dict, target: Request, snapshot: dict) -> None:
        fields = ordered_fields(target)
        report, pages = snapshot["profile"], snapshot["web_pages"]
        if not report.get("related_passages"):
            index = runbook.PassageIndex(web_archive.research_documents(report, pages))
            passages = workflow.retrieve_field_passages(
                index, fields, report["fields"], report["as_of"]
            )
            report = {
                **report,
                "related_passages": {key: rows for key, rows in passages.items() if rows},
            }
        profile = curated(fields, report, target.company, pages)
        if segment := settings.get("segment"):
            profile["segment"] = segment
        evidence_ids = {ref["id"] for ref in profile["evidence"]}
        evidence = {
            "field_report_version": field_report.VERSION,
            "source_links": {
                ref: doc["url"]
                for doc in report["source_outcomes"]
                if (ref := field_report.evidence_id(doc["url"])) in evidence_ids
            },
            "web_inputs": web_archive.inputs(
                self._page_items(pages, row["company_id"]), profile["evidence"]
            ),
            "field_results": field_results(report["fields"], report.get("related_passages")),
        }
        digest = sha256(json.dumps([profile, evidence], sort_keys=True).encode()).hexdigest()
        content = markdown(fields, report, target.company, profile)
        content += f"\n<!-- GTM catalog metadata {digest} -->\n"
        revision = sha256(content.encode()).hexdigest()
        gtm_catalog.prepare(self.state, self.source.name, row, revision, profile, content, evidence)

    def _collect(
        self,
        target: Request,
        *,
        row: dict | None = None,
        catalog_progress: tuple[int, int] | None = None,
    ) -> dict:
        name = self.source.name
        key = sha256(target.model_dump_json().encode()).hexdigest()
        record = json.loads(row["state_json"]) if row else gtm_collections.load(self.state, name)
        save = (
            partial(gtm_catalog.checkpoint, self.state, name, row)
            if row
            else partial(gtm_collections.save, self.state, name)
        )
        previous = record.get("snapshot") or {}
        mode = self.sync_mode or "incremental"
        pending = record.get("pending") or {}
        if (
            pending.get("key") != key
            or pending.get("mode") != mode
            or pending.get("run", {}).get("version") != workflow.CHECKPOINT_VERSION
        ):
            run = workflow.initialize(target)
            # Managed syncs publish deterministic context. They never build a
            # passage packet for an individual run's reviewer.
            run["review_passages"] = {}
            pending = {
                "key": key,
                "mode": mode,
                "run": run,
                "parsed": {},
                "failed_reads": [],
            }
        run = pending["run"]
        parsed = pending["parsed"]
        captures = pending.setdefault("web_captures", {})
        responses = {}
        old_parsed = previous.get("parsed", {}) if mode == "incremental" else {}
        failed_reads = set(pending["failed_reads"])

        def reader(url: str, robots: dict) -> tuple[str, str, str]:
            try:
                result = fetch(url, robots)
            except FetchError as exc:
                if exc.retryable:
                    failed_reads.add(url)
                raise
            failed_reads.discard(url)
            final_url, response_type, body = result
            responses[final_url] = (response_type, body)
            return result

        while run["phase"] != "complete":
            cancel_scope.check()
            with reuse_parsed(old_parsed, parsed):
                try:
                    workflow.advance(run, reader=reader)
                except IsolationUnavailable as exc:
                    raise ConnectorUnavailable(str(exc)) from exc
            for url, (response_type, body) in responses.items():
                doc = run["documents"].get(url)
                if doc and not doc["quarantined"]:
                    web_archive.capture(self.media_store, captures, url, response_type, body)
            responses.clear()
            pending["failed_reads"] = sorted(failed_reads)
            save({"snapshot": previous, "pending": pending})
            if self.listing_progress:
                attempts = sum(task["attempts"] for task in run["tasks"])
                detail = (
                    "waiting for publisher retry window"
                    if run.get("next_step_at")
                    else "indexing company passages and checking field coverage"
                    if run["phase"] == "research"
                    else f"{attempts} page {'attempt' if attempts == 1 else 'attempts'} saved"
                )
                if catalog_progress and row:
                    current, total = catalog_progress
                    self.listing_progress(
                        "researching", current, total, f"{row['company_id']} · {detail}"
                    )
                else:
                    self.listing_progress("researching", attempts, None, detail)
            if retry_at := run.get("next_step_at"):
                cancel_scope.sleep(max(0, retry_at - time.time()))
        if (run["identity"].get("selected") or {}).get("fallback_url"):
            # Identity alternatives can recover a failed website. Failures in
            # the following evidence collection still obey allow_partial.
            failed_reads.difference_update(
                task["url"] for task in run["tasks"] if task["role"] in workflow.IDENTITY_ROLES
            )
        if (failed_reads and not target.acquisition.allow_partial) or run["identity"][
            "status"
        ] != "matched":
            save({"snapshot": previous, "failed": True})
            raise ConnectorUnavailable(
                "GTM refresh could not verify its target or had unresolved transient page failures"
            )
        report = workflow.profile(run)
        fields = ordered_fields(target)
        pages = web_archive.admitted(workflow.docs(run), captures)
        if failed_reads and previous.get("target_key") == key:
            # A paced/partial refresh can keep a previously approved document
            # on a transient fetch error. A quarantine or invalid identity
            # is not a retryable read and cannot enter this path.
            for url in failed_reads:
                ref = web_archive.reference(url)
                if ref in previous.get("web_pages", {}) and url not in run["documents"]:
                    pages.setdefault(ref, previous["web_pages"][ref])
        profile = curated(fields, report, target.company, pages)
        content = markdown(fields, report, target.company, profile)
        if row is None:
            self._single_inputs = web_archive.inputs(self._page_items(pages), profile["evidence"])
            if self._single_inputs:
                encoded = json.dumps(self._single_inputs, sort_keys=True).encode()
                digest = sha256(encoded).hexdigest()
                content += f"\n<!-- GTM web inputs {digest} -->\n"
        snapshot = {
            "revision": sha256(content.encode()).hexdigest(),
            "markdown": content,
            "parsed": parsed,
            "profile": report,
            "acquisition": presentation.acquisition(run),
            "page_tasks": run["tasks"],
            "token_usage": presentation.token_usage(run),
            "target_key": key,
            "web_pages": pages,
        }
        save({"snapshot": snapshot})
        return snapshot

    def read_item(self, item: ConnectorItem) -> ConnectorPayload:
        if ref := item.metadata.get("page_reference"):
            page = (
                gtm_catalog.page(self.state, self.source.name, item.metadata["company_id"], ref)
                if self.catalog
                else self._snapshot["web_pages"].get(ref, {})
            )
            return web_archive.payload(item, page, self.media_store)
        if self.catalog:
            saved = gtm_catalog.publication(
                self.state, self.source.name, item.metadata["company_id"], item.sha256
            )
            if not saved:
                raise ConnectorUnavailable("Prepared GTM publication is missing")
            profile = json.loads(saved["profile_json"])
            return ConnectorPayload(
                item,
                saved["markdown"].encode(),
                mime_type=item.mime_type,
                sha256=item.sha256,
                metadata={
                    "extension_provenance": [
                        {**ref, "label": f"{ref['publisher']} · {ref['kind']}"}
                        for ref in profile["evidence"]
                    ],
                    "extension_inputs": json.loads(saved["evidence_json"]).get("web_inputs", {}),
                },
            )
        return ConnectorPayload(
            item,
            self._snapshot["markdown"].encode("utf-8"),
            mime_type=item.mime_type,
            sha256=item.sha256,
            metadata={"extension_inputs": self._single_inputs},
        )

    def validate(self) -> ConnectorHealth:
        if self.catalog:
            count = len(gtm_catalog.listings(self.state, self.source.name))
            return ConnectorHealth(True, "healthy", count, 0, checkpoint=self.get_checkpoint())
        try:
            self._target()
        except ConnectorUnavailable as exc:
            return ConnectorHealth(False, "unhealthy", 0, 0, errors=[str(exc)])
        return ConnectorHealth(True, "healthy", 1, 0, checkpoint=self.get_checkpoint())

    def checkpoint_from_items(self, items):
        cursor, watermark = super().checkpoint_from_items(items)
        if self.catalog:
            pages = sum("page_reference" in item.metadata for item in items)
            return cursor, {**watermark, "gtm": {"companies": len(items) - pages, "pages": pages}}
        profile = self._snapshot["profile"]
        cursor["revision"] = self._snapshot["revision"]
        watermark["gtm"] = {
            "sources": len(profile["source_outcomes"]),
            "quarantined": sum(row["quarantined"] for row in profile["source_outcomes"]),
            "field_status_counts": profile["field_status_counts"],
        }
        return cursor, watermark
