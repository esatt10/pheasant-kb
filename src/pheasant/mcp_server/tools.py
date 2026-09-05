from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

from pheasant.config.loader import dump_config_yaml
from pheasant.config.schema import PheasantConfig, SourceConfig, SourceType
from pheasant.graph.query_service import graph_for_config
from pheasant.ingestion.pipeline import utc_now
from pheasant.jobs import JobRegistry
from pheasant.mcp_server.readiness_tools import ReadinessTools
from pheasant.persistence.paths import StatePaths
from pheasant.persistence.state_store import StateStore
from pheasant.registry.knowledge_base_registry import KnowledgeBaseRegistry
from pheasant.registry.source_registry import SourceRegistry
from pheasant.search.criteria import (
    apply_retrieval_criteria,
    source_of,
)
from pheasant.search.hybrid import HybridSearch
from pheasant.search.ranking import resolver_for
from pheasant.search.sqlite_store import SearchStore
from pheasant.security.path_policy import resolve_config_write_target, resolve_under
from pheasant.services import ServiceContext
from pheasant.services import graph as graph_service
from pheasant.services import retrieval as retrieval_service
from pheasant.services.errors import SourceNotFound
from pheasant.sync.engine import SyncEngine

logger = logging.getLogger(__name__)

#: Step 33.6 — the criteria filters moved to `pheasant.search.criteria` so
#: `POST /search` and the MCP tool share one implementation instead of HTTP
#: silently having fewer filters. Applying them moved again in 35.9, into
#: `services.retrieval.search`, so neither surface decides when they run
#: either. `apply_retrieval_criteria` stays importable from this module —
#: callers and tests reach for it here — and `_source_of` keeps its old
#: private name for the same reason.
__all__ = ["PheasantTools", "apply_retrieval_criteria", "source_of"]

_source_of = source_of


def _preview_rows(results: list[dict]) -> list[dict]:
    """Compact hits for a preview: enough to judge relevance, not the corpus."""
    rows = []
    for item in results:
        rows.append(
            {
                "id": item.get("id") or item.get("node_id"),
                "path": item.get("relative_path") or item.get("path"),
                "source_id": _source_of(item) or None,
                "type": item.get("type"),
                "score": item.get("score"),
                "preview": (str(item.get("text") or item.get("summary") or ""))[:200],
            }
        )
    return rows


class PheasantTools(ReadinessTools):
    def __init__(self, config: PheasantConfig):
        self.config = config
        self.paths = StatePaths.from_config(config)
        self.paths.ensure()
        self.state = StateStore.from_config(config, self.paths.sqlite)
        self.state.migrate()
        self._remote_graph = bool(str(config.graph.query_service_url or "").strip())
        self.engine = SyncEngine(
            config,
            self.paths,
            self.state,
            load_persisted_graph=not self._remote_graph,
        )
        self.graph = graph_for_config(config, lambda: self.engine.graph_builder.graph)
        # MCP and A2A callers cannot consume the HTTP server's in-process job
        # registry.  Keep the same progress contract on the tool facade so an
        # agent can start a long first index, retain the job id, and inspect it
        # without holding a tool call open for minutes.
        self.jobs = JobRegistry()
        self._job_lock = threading.Lock()
        self.searcher = HybridSearch(
            SearchStore(self.state, ranking=resolver_for(config, self.state)),
            vector=self.engine.vector_searcher(),
            node_index=self.engine.node_index,
            wasm_relationship_search=config.search.wasm_relationship_search,
            steering_enabled=config.memory.steering_enabled,
            default_memory_policy=config.memory.default_policy,
            usage_tracking=config.memory.usage_tracking,
            stage_sample_rate=config.observability.interactions.stage_sample_rate,
        )
        # The same application layer the HTTP surface calls, assembled the same
        # way. Operations that exist on both surfaces are one function taking
        # this; what remains on either side is transport.
        self.services = ServiceContext(
            config=config,
            state=self.state,
            searcher=self.searcher,
            graph=self.graph,
            engine=self.engine,
        )

    def _publish_sync(
        self,
        source_name: str,
        mode: str,
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Route fleet MCP writes through the same durable queue as HTTP."""

        from pheasant.sync.queue import IndexTask, queue_from_config

        payload = {"max_depth": max_depth, "full_scan": bool(full_scan)}
        digest = hashlib.sha256(
            (
                f"{self.config.knowledge_base_id}\0{source_name}\0{mode}\0"
                + json.dumps(payload, sort_keys=True, separators=(",", ":"))
            ).encode()
        ).hexdigest()[:24]
        task = IndexTask(
            id=f"idx-{digest}",
            source_id=source_name,
            mode=mode,
            payload=payload,
            max_attempts=max(1, int(self.config.sync.queue.max_attempts or 1)),
        )
        queue = queue_from_config(self.config, self.state)
        if queue is None:
            raise RuntimeError("remote graph fleet needs an enabled sync queue")
        try:
            queue.publish(task)
        finally:
            queue.close()
        return {"source_id": source_name, "status": "queued", "task_id": task.id}

    def list_knowledge_bases(self) -> dict:
        return {"knowledge_bases": KnowledgeBaseRegistry(self.state).list()}

    def register_source(
        self,
        knowledge_base: str,
        name: str,
        source_type: str,
        path: str,
        description: str | None = None,
        enabled: bool = True,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
        taxonomy: bool = False,
        actor: str = "mcp",
        transport: str = "mcp",
        client_id: str | None = None,
        sync_now: bool = False,
        wait: bool = False,
        sync_mode: str = "incremental",
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        if self.config.security.allow_user_selected_source_paths:
            resolved_path = Path(path).expanduser().resolve()
            if not resolved_path.exists():
                raise ValueError(f"Path does not exist: {resolved_path}")
        else:
            resolved_path = resolve_under(
                path,
                [
                    self.config.pheasant.workspace_root,
                    self.config.pheasant.exports_path,
                    *self.config.security.allow_workspace_roots,
                ],
            )
        source = SourceConfig(
            name=name,
            type=SourceType(source_type),
            path=resolved_path,
            description=description,
            enabled=enabled,
        )
        if include is not None:
            source.include = include
        if exclude is not None:
            source.exclude = exclude
        if taxonomy:
            # Structural taxonomy extraction (chapters/sections/§ codes) for
            # books, procedures and legal documents. Additive optional
            # parameter: an agent that never passes it registers exactly the
            # source it did before (rule 8 — additive evolution only).
            source.taxonomy.enabled = True
        SourceRegistry(self.config, self.state).register_source(source)
        self.config.sources = [
            existing for existing in self.config.sources if existing.name != source.name
        ]
        self.config.sources.append(source)
        created_at = utc_now()
        self._audit(
            source.name,
            "register_source",
            actor,
            transport,
            client_id,
            created_at,
            {"source": source.model_dump(mode="json")},
        )
        response = {
            "status": "registered",
            "knowledge_base": self.config.knowledge_base_id,
            "source": source.model_dump(mode="json"),
            "provenance": {
                "registered_by": actor,
                "registered_at": created_at,
                "transport": transport,
                "client_id": client_id,
                "persistence": "runtime_state",
                "config_update_required": True,
            },
        }
        if sync_now:
            if wait:
                response["sync_result"] = self.sync_source(knowledge_base, source.name, sync_mode)
            else:
                response["job"] = self.start_sync_source(knowledge_base, source.name, sync_mode)
        return response

    def start_sync_source(
        self,
        knowledge_base: str,
        source_name: str,
        mode: str = "incremental",
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        """Start indexing and immediately return a followable job record."""
        self._require_knowledge_base(knowledge_base)
        self._require_source(source_name)
        with self._job_lock:
            active = self.jobs.active_for(source_name)
            if active is not None:
                return active.as_dict()
            job = self.jobs.create("sync", f"Indexing {source_name}", [source_name])

        if self._remote_graph:
            result = self._publish_sync(source_name, mode, max_depth, full_scan)
            self.jobs.finish(job.id, "succeeded", result=result)
            return self.jobs.get(job.id) or job.as_dict()

        def run() -> None:
            def progress(phase: str, current: int, total: int | None, detail: str) -> None:
                self.jobs.progress(
                    job.id,
                    phase=phase,
                    current=current,
                    total=total,
                    detail=detail,
                )

            try:
                result = self.engine.sync_source(
                    source_name,
                    mode,  # type: ignore[arg-type]
                    max_depth=max_depth,
                    full_scan=full_scan,
                    on_progress=progress,
                ).__dict__
            except Exception as exc:  # background tool work cannot propagate
                self.jobs.finish(job.id, "failed", error=str(exc))
                return
            failed = result.get("status") in {"failed", "timeout", "limit_exceeded"}
            self.jobs.finish(
                job.id,
                "failed" if failed else "succeeded",
                error=(result.get("details") or {}).get("error") if failed else None,
                result=result,
            )

        threading.Thread(
            target=run,
            name=f"pheasant-mcp-sync-{source_name}",
            daemon=True,
        ).start()
        return job.as_dict()

    def get_job(self, job_id: str) -> dict:
        """Return current progress and the terminal result for one tool job."""
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(f"Unknown job: {job_id}")
        return job

    def list_jobs(self, active_only: bool = False, limit: int = 50) -> dict:
        """List recent tool jobs, with active indexing jobs first."""
        return {"jobs": self.jobs.list(active_only=active_only, limit=limit)}

    def list_sources(
        self,
        knowledge_base: str,
        enabled: bool | None = None,
        status: str | None = None,
        source_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        return {
            "sources": SourceRegistry(self.config, self.state).list_sources(
                enabled=enabled,
                status=status,
                source_type=source_type,
                limit=limit,
                offset=offset,
            ),
            "pagination": {"limit": limit, "offset": offset},
        }

    def disable_source(
        self,
        knowledge_base: str,
        source_name: str,
        actor: str = "mcp",
        transport: str = "mcp",
        client_id: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        self._require_source(source_name)
        self.state.set_source_enabled(source_name, False, "disabled")
        for source in self.config.sources:
            if source.name == source_name:
                source.enabled = False
        self._audit(source_name, "disable_source", actor, transport, client_id, utc_now())
        return {"status": "disabled", "source_name": source_name}

    def remove_source(
        self,
        knowledge_base: str,
        source_name: str,
        actor: str = "mcp",
        transport: str = "mcp",
        client_id: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        self._require_source(source_name)
        if self._remote_graph:
            raise RuntimeError(
                "source removal changes the graph and must run on the indexer/control plane"
            )
        self.engine.graph_builder.remove_source_content(source_name)
        self.engine.graph_store.save(self.config.knowledge_base_id, self.engine.graph_builder.graph)
        self.engine.manifests.delete(source_name)
        self.state.delete_source(source_name)
        self.config.sources = [
            source for source in self.config.sources if source.name != source_name
        ]
        self._audit(source_name, "remove_source", actor, transport, client_id, utc_now())
        return {"status": "removed", "source_name": source_name}

    def promote_runtime_source_to_config(
        self,
        knowledge_base: str,
        source_name: str,
        config_path: str | None = None,
        write: bool = False,
        actor: str = "mcp",
        transport: str = "mcp",
        client_id: str | None = None,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        source = self._source_config(source_name)
        source_payload = source.model_dump(mode="json")
        yaml_patch = dump_config_yaml({"sources": [source_payload]})
        wrote = False
        if write:
            if not config_path:
                raise ValueError("config_path is required when write=True")
            # An agent-supplied path must not become an arbitrary file write:
            # promotion may only touch this region's own config, or a path
            # under an allowed workspace root.
            path = resolve_config_write_target(
                config_path,
                server_config_path=os.environ.get("PHEASANT_CONFIG", "/config/pheasant.yaml"),
                allowed_roots=[
                    self.config.pheasant.workspace_root,
                    *self.config.security.allow_workspace_roots,
                ],
            )
            data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(data, dict):
                raise ValueError(
                    f"Refusing to overwrite {path}: it is not a pheasant config mapping"
                )
            existing = [
                item for item in data.get("sources", []) or [] if item.get("name") != source.name
            ]
            data["sources"] = [*existing, source_payload]
            path.write_text(dump_config_yaml(data), encoding="utf-8")
            wrote = True
        self._audit(
            source_name,
            "promote_runtime_source_to_config",
            actor,
            transport,
            client_id,
            utc_now(),
            {"write": wrote, "config_path": config_path},
        )
        return {
            "status": "promoted" if wrote else "patch_generated",
            "source_name": source_name,
            "yaml_patch": yaml_patch,
            "wrote_config": wrote,
            "config_path": config_path,
        }

    def scan_source(
        self,
        knowledge_base: str,
        source_name: str,
        max_depth: int | None = None,
    ) -> dict:
        """Estimate what a source would index, without indexing anything.

        Call this before syncing a source that points at something large or
        unfamiliar (a home directory, a whole drive). Reports the file count,
        total size, the largest subtrees, how many files each depth cap would
        admit, and whether the configured limits would refuse the sync.
        Reads no file content and writes nothing.
        """
        self._require_knowledge_base(knowledge_base)
        return self.engine.scan_source(source_name, max_depth=max_depth)

    def sync_source(
        self,
        knowledge_base: str,
        source_name: str,
        mode: str = "incremental",
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        if self._remote_graph:
            result = self._publish_sync(source_name, mode, max_depth, full_scan)
            self._audit(source_name, "sync_source", "mcp", "mcp", None, utc_now(), result)
            return result
        result = self.engine.sync_source(
            source_name,
            mode,  # type: ignore[arg-type]
            max_depth=max_depth,
            full_scan=full_scan,
        ).__dict__
        self._audit(source_name, "sync_source", "mcp", "mcp", None, utc_now(), result)
        return result

    def sync_all(
        self,
        knowledge_base: str,
        mode: str = "incremental",
        max_depth: int | None = None,
        full_scan: bool = False,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        if self._remote_graph:
            results = [
                self._publish_sync(source.name, mode, max_depth, full_scan)
                for source in self.engine.enabled_sources()
            ]
            for result in results:
                self._audit(
                    result["source_id"], "sync_source", "mcp", "mcp", None, utc_now(), result
                )
            return {"results": results}
        results = [
            r.__dict__
            for r in self.engine.sync_all(
                mode,  # type: ignore[arg-type]
                max_depth=max_depth,
                full_scan=full_scan,
            )
        ]
        for result in results:
            self._audit(result["source_id"], "sync_source", "mcp", "mcp", None, utc_now(), result)
        return {"results": results}

    def memory_write(
        self,
        knowledge_base: str,
        text: str,
        scope: str = "user",
        subject: str | None = None,
        supersedes: str | None = None,
        tags: list[str] | None = None,
        sync: bool = True,
        kind: str = "fact",
        principal: str | None = None,
        valid_until: str | None = None,
    ) -> dict:
        """Append one memory record and (by default) index it immediately.

        The record lands as a Markdown file in the configured ``type: memory``
        source; ``sync=True`` runs an incremental sync of that source so the
        memory is retrievable via ``search_context`` in the same session
        (read-your-writes). Recall is ordinary search — no separate path.

        ``principal`` records *who asserted this* — it is what lets a later
        step scope a user's memories to that user rather than to everyone
        (Step 33.11), and it is part of the record id, so two agents writing
        the same sentence in the same second get two records instead of
        silently sharing one. ``kind`` marks a record as retrieval policy
        rather than a fact; ``valid_until`` gives it an expiry it declares
        itself. All three are optional and default to the pre-33.5 behavior
        (CLAUDE.md §4 rule 8: additive only).
        """
        from pheasant.memory.reinforcement import StateReinforcementIndex
        from pheasant.memory.store import MemoryStore, memory_source

        self._require_knowledge_base(knowledge_base)
        source = memory_source(self.config, self.state)
        if source is None:
            raise ValueError(
                "no enabled memory source configured. Add one to pheasant.yaml:\n"
                "  sources:\n"
                "    - name: agent-memory\n"
                "      type: memory\n"
                "      path: memory"
            )
        # Phase 1: a write whose *normalized* text already matches a live
        # record in the same (scope, subject, kind, ACL) bucket reinforces
        # that record instead of creating a new one. `reinforcement_enabled`
        # defaults on (see MemorySettings) but stays a real knob, and a
        # cold/absent projection degrades `find()` to "no match" — never
        # blocks the write.
        reinforcement = (
            StateReinforcementIndex(self.state)
            if self.config.memory.reinforcement_enabled
            else None
        )
        store = MemoryStore(source.path)
        record, created = store.append(
            text,
            scope=scope,
            subject=subject,
            supersedes=supersedes,
            tags=tags or (),
            kind=kind,
            written_by=principal,
            valid_until=valid_until,
            reinforcement=reinforcement,
        )
        outcome = store.last_outcome or ("created" if created else "duplicate")

        from pheasant.telemetry import metrics as _metrics

        _metrics.record_memory_write(outcome, store.last_fold)
        result: dict = {
            "record": record.as_dict(),
            "created": created,
            "source": source.name,
            # Additive (rule 8): "created" | "reinforced" | "duplicate".
            # `created` is unchanged and still means exactly what it always
            # has; `outcome` distinguishes the two ways `created=False` can
            # now happen.
            "outcome": outcome,
        }
        submitted = (text or "").strip()
        if not created and submitted and submitted != record.text:
            # Only possible for outcome="reinforced": an exact-digest match
            # (outcome="duplicate") is byte-identical to `record.text` by
            # construction. The caller gets back a record whose stored text
            # is not what it wrote — worth surfacing rather than leaving
            # implicit.
            result["submitted_text"] = submitted
        # The record is already durably on disk; this sync only makes it
        # *searchable now*. Failing the whole request when it cannot run — most
        # often because another writer holds the engine lease, which a live run
        # hit immediately — reports "your memory was not saved" when it was.
        # Degrade to deferred indexing instead: the scheduler and the next sync
        # both pick it up.
        if sync and created:
            if self._remote_graph:
                result["sync"] = self._publish_sync(source.name, "incremental")
            else:
                try:
                    # Avoid making an interactive write pay the whole-graph
                    # enrichment walk. The next ordinary sync clears the
                    # persisted dirty marker and completes enrichment.
                    result["sync"] = self.engine.sync_source(
                        source.name, "incremental", enrich="deferred"
                    ).__dict__
                except Exception as exc:
                    logger.warning("memory write indexed later: %s", exc)
                    result["sync_deferred"] = str(exc)
        self._audit(
            source.name,
            "memory_write",
            principal or "mcp",
            "mcp",
            None,
            utc_now(),
            {
                "record_id": record.record_id,
                "scope": record.scope,
                "kind": record.kind,
                "created": created,
            },
        )
        return result

    def memory_list(
        self, knowledge_base: str, scope: str | None = None, current_only: bool = True
    ) -> dict:
        """This region's memory records, newest last.

        Backs the `pheasant://…/memory` resource. `current_only` defaults to
        **True** here, unlike `GET /memory`, because a resource is context an
        agent reads directly: handing it corrected records without the
        supersedes chain to interpret them would be worse than handing it
        nothing.
        """
        from pheasant.memory.store import MemoryStore, memory_source

        self._require_knowledge_base(knowledge_base)
        source = memory_source(self.config, self.state)
        if source is None:
            return {"enabled": False, "records": []}
        records = MemoryStore(source.path).list_records(scope, current_only=current_only)
        # Phase 3: tier/subsumed_by are SQL-only (earned by a compaction
        # pass, not a file field), so a record's file-based `as_dict()` is
        # annotated with them here rather than the store growing a state
        # handle. Best-effort — a cold/absent projection just means every
        # record reports the pre-Phase-3 default (hot, unsubsumed), same as
        # a region that has never run compaction.
        try:
            compaction = {str(row["record_id"]): row for row in self.state.memory_compaction_rows()}
        except Exception:
            compaction = {}
        out = []
        for record in records:
            payload = record.as_dict()
            row = compaction.get(record.record_id)
            payload["tier"] = str(row["tier"]) if row and row.get("tier") else "hot"
            payload["subsumed_by"] = row.get("subsumed_by") if row else None
            out.append(payload)
        return {
            "enabled": True,
            "source": source.name,
            "records": out,
        }

    def list_memory_candidates(
        self,
        knowledge_base: str,
        status: str = "pending",
        rule_id: str | None = None,
        principal: str | None = None,
        max_results: int = 50,
    ) -> dict:
        """List memory the region has *proposed* but nothing has admitted yet.

        These are not memories. Nothing here is retrievable, and nothing
        becomes retrievable until it is promoted --- which is the whole reason
        a region can learn from how it is used without a UI session's traffic
        quietly turning into knowledge.

        Each candidate carries the evidence behind it: which rule proposed it,
        how many times the pattern was observed, and across how many sessions.
        Read that before promoting; a proposal is a suggestion, not a finding.
        """

        self._require_knowledge_base(knowledge_base)
        return {
            "candidates": self.engine.state.list_memory_candidates(
                status=status or None,
                rule_id=rule_id,
                principal=principal,
                limit=max(1, min(int(max_results), 200)),
            ),
            "counts": self.engine.state.memory_candidate_counts(),
        }

    def promote_memory_candidate(
        self, knowledge_base: str, candidate_id: str, principal: str | None = None
    ) -> dict:
        """Admit one proposal, making it an ordinary memory record.

        The record is written through the same path `memory_write` uses and
        indexed by the same pipeline, so it competes for a slot on the same
        ranking as anything else --- it simply was not typed by anyone.
        """

        from pheasant.memory.formation import admit

        self._require_knowledge_base(knowledge_base)
        return admit(self.engine, candidate_id, admitted_by=principal or "mcp")

    def reject_memory_candidate(
        self, knowledge_base: str, candidate_id: str, principal: str | None = None
    ) -> dict:
        """Decline one proposal, permanently.

        The rule that proposed it will not suggest it again.
        """

        from pheasant.memory.formation import reject

        self._require_knowledge_base(knowledge_base)
        return reject(self.engine, candidate_id, rejected_by=principal or "mcp")

    def memory_consolidate(self, knowledge_base: str) -> dict:
        """Run one memory-consolidation pass now (Step 33.2).

        Archives superseded and TTL-expired records (files renamed, never
        deleted) and re-syncs the memory source so they leave the index. The
        scheduler runs this automatically; this tool is the on-demand edge.
        """
        from pheasant.memory.maintenance import run_memory_maintenance

        self._require_knowledge_base(knowledge_base)
        result = run_memory_maintenance(self.engine)
        if result is None:
            return {
                "skipped": (
                    "memory consolidation is disabled or no `type: memory` source is configured"
                )
            }
        self._audit(
            result["source"], "memory_consolidate", "mcp", "mcp", None, utc_now(), result["report"]
        )
        return result

    def memory_synthesize(self, knowledge_base: str) -> dict:
        """Run one LLM-merge pass now (Phase 4). Off by default and never
        automatic — see `MemorySynthesisSettings`. Merges a near-duplicate
        cluster deterministic compaction could not resolve into one new
        canonical record, subsuming the originals exactly as medoid
        promotion does. Returns `{"skipped": reason}` when synthesis is
        disabled, no `type: memory` source is configured, or no model is
        reachable.
        """
        from pheasant.memory.store import MemoryStore, memory_source
        from pheasant.memory.synthesis import run_synthesis

        self._require_knowledge_base(knowledge_base)
        source = memory_source(self.config, self.state)
        if source is None:
            return {"skipped": "no `type: memory` source is configured"}
        records = MemoryStore(source.path).list_records()
        result = run_synthesis(self.engine, records, self.config.memory.synthesis, source)
        self._audit(source.name, "memory_synthesize", "mcp", "mcp", None, utc_now(), result)
        return result

    def search_context(
        self,
        knowledge_base: str,
        query: str,
        mode: str = "hybrid",
        max_results: int = 10,
        include_chunks: bool = True,
        include_graph_neighbors: bool = True,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        section: str | None = None,
        source_name: str | None = None,
        exclude_sources: list[str] | None = None,
        node_types: list[str] | None = None,
        min_score: float | None = None,
        memory: Any = None,
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
    ) -> dict:
        """Retrieve passages for a query.

        The trailing parameters are **retrieval criteria** an agent can set per
        call rather than having them fixed in config: restrict to one section
        of a document's taxonomy, scope to one source or away from noisy ones,
        keep only certain node types, floor the score, and decide how this
        region's agent memory participates. All are optional and default to the
        pre-existing behavior, so an existing caller is unaffected
        (CLAUDE.md §4 rule 8: additive only).

        ``source_types``/``exclude_source_types`` scope by the *kind* of
        source — ``repository``, ``notion``, ``slack``, ``markdown_folder`` and
        so on — rather than by name. Scoping by name means knowing every source
        in the region first; scoping by type is the question an agent usually
        has ("only what came from our wikis"). Every hit reports its own under
        ``provenance.source_type``, and ``describe_retrieval`` lists the types
        this region actually holds.

        ``section`` matches the heading breadcrumb, so "§ 12.3", "Article IV"
        or a section's wording all reach it and naming a parent returns
        everything nested under it. Only meaningful for sources with taxonomy
        extraction enabled.

        ``memory`` takes one word — ``"off"``, ``"only"``, ``"prefer"`` or the
        default ``"auto"`` — or an object for the rest:
        ``{"scopes": ["user"], "subject": "deploy", "as_of": "2026-01-01T00:00:00Z",
        "current_only": false}``. Corrected records are dropped by default, so
        you do not have to wait for a consolidation pass to stop seeing a fact
        the region already knows was superseded; ``as_of`` deliberately brings
        them back, for asking what was believed at a past instant. Hits that
        came from memory carry a ``memory`` block with the record's scope,
        subject and when it was asserted.
        """
        self._require_knowledge_base(knowledge_base)
        # Transport adapter. The operation is `services.retrieval.search`:
        # over-fetch, criteria, the memory policy, the metric and the graph
        # generation are decided there, so the HTTP route of the same name
        # cannot answer differently. Before that, this tool computed its own
        # over-fetch and never recorded a search metric at all.
        return retrieval_service.search(
            self.services,
            retrieval_service.SearchRequest(
                query=query,
                knowledge_base=knowledge_base,
                mode=mode,
                max_results=max_results,
                source_name=source_name,
                section=section,
                principal=principal,
                principal_groups=principal_groups,
                memory=memory,
                exclude_sources=exclude_sources,
                node_types=node_types,
                min_score=min_score,
                source_types=source_types,
                exclude_source_types=exclude_source_types,
            ),
        )

    def record_evidence(
        self,
        knowledge_base: str,
        query: str,
        target_id: str,
        event_type: str,
        target_type: str = "artifact",
        principal: str | None = None,
        session_id: str | None = None,
        position: int | None = None,
        outcome_reference: str | None = None,
    ) -> dict:
        """Record what actually came of a result this region returned.

        The one way an agent contributes *proof* rather than traffic. Retrieval
        already records what was served; only the caller knows whether it was
        cited, selected, accepted, rejected, or validated by something
        deterministic -- and those are four claims of very different strength,
        which is why ``event_type`` is required and drawn from a fixed
        taxonomy rather than being a "useful: true" flag.

        Two defaults worth knowing before instrumenting anything: being served
        is *not* evidence of usefulness, and *not* selecting something is not
        evidence against it. Both stay unknown, so an agent that reports
        nothing degrades the evaluation's coverage rather than corrupting its
        conclusions.

        ``pheasant://evaluation/taxonomy`` lists every event type.
        """

        self._require_knowledge_base(knowledge_base)
        import pheasant.evaluation as evaluation

        return evaluation.record_evidence(
            self.state,
            self.config,
            query=query,
            target_id=target_id,
            target_type=target_type,
            event_type=event_type,
            principal=principal,
            session_id=session_id,
            position=position,
            outcome_reference=outcome_reference,
            reason_code="mcp",
        )

    def get_evaluation_report(self, knowledge_base: str, run_id: str | None = None) -> dict:
        """The most recent evidence-bearing effectiveness report, or a named one.

        Returns the agent projection: status, evidence sufficiency, the metric
        deltas, the gates, the limitations, and the actions this report permits.
        The full report -- per-query operands, formulas, proof references -- is
        available over HTTP at ``/evaluation/report``; what comes back here is
        the part an agent can act on without parsing a document.
        """

        self._require_knowledge_base(knowledge_base)
        import pheasant.evaluation as evaluation
        from pheasant.evaluation import store as evaluation_store

        report = (
            evaluation_store.load_report(self.state, run_id)
            if run_id
            else evaluation.latest_report(self.state, self.config.knowledge_base_id)
        )
        if report is None:
            raise ValueError(
                f"No evaluation report for {run_id}"
                if run_id
                else "No evaluation run has completed for this knowledge base yet"
            )
        return {
            "run_identity": report.get("run_identity", {}),
            "health_vector": report.get("health_vector", {}),
            "gates": report.get("gates", []),
            "generalization": report.get("generalization", {}),
            "candidate_decisions": report.get("candidate_decisions", []),
            "limitations": report.get("limitations", {}),
            "agent": (report.get("explanations") or {}).get("agent", {}),
        }

    def start_evaluation(
        self,
        knowledge_base: str,
        mode: str = "current_state",
        as_of: str | None = None,
        force: bool = False,
    ) -> dict:
        """Start an effectiveness batch and return immediately with its run id.

        A batch replays every cohort under every variant through the real search
        path — minutes of work on a real corpus, far past what a tool call
        should hold open. So this starts it and hands back a ``run_id``; poll
        :meth:`get_evaluation_status` to watch it.

        Progress and completion live in ``/state``, not in this process, which
        is what makes the answer survive the container being restarted and what
        lets a different process (the UI, a CLI, another agent) watch the same
        run. A batch also takes the region's evaluation lease, so asking twice
        gets you the run already in flight rather than a second one.
        """

        self._require_knowledge_base(knowledge_base)
        import threading

        import pheasant.evaluation as evaluation

        if not self.config.evaluation.enabled and not force:
            raise ValueError(
                "Evaluation is disabled for this knowledge base. Enable "
                "`evaluation.enabled` in pheasant.yaml, or pass force=true for a "
                "one-off run."
            )
        in_flight = evaluation.progress(self.state, self.config.knowledge_base_id)
        if in_flight.get("status") == "running":
            return {"status": "already-running", **in_flight}

        job = self.jobs.create("evaluation", f"evaluation ({mode})", targets=["evaluation"])

        def _run() -> None:
            try:
                outcome = evaluation.run(
                    self.engine,
                    mode=mode,
                    effective_as_of=as_of,
                    force=bool(force),
                    on_progress=lambda phase, detail: self.jobs.progress(
                        job.id, phase=phase, detail=detail or phase, source="evaluation"
                    ),
                )
                self.jobs.finish(
                    job.id,
                    status="succeeded" if outcome.status != "invalid" else "failed",
                    result={"run_id": outcome.run_id, "status": outcome.status},
                )
            except Exception as exc:  # noqa: BLE001 - a job records its own failure
                self.jobs.finish(job.id, status="failed", error=str(exc))

        threading.Thread(target=_run, name="pheasant-mcp-evaluation", daemon=True).start()
        return {"status": "started", "job_id": job.id, "mode": mode}

    def get_evaluation_status(self, knowledge_base: str, run_id: str | None = None) -> dict:
        """How far a batch has got, and whether anything is running at all.

        Read from ``/state``, so it answers for a run this process did not start
        and for one whose process has since stopped. ``phase`` names the step
        (`snapshot`, `cohorts`, `proof`, `variants`, `replay`, `metrics`,
        `gates`, `report`, `persisting`), ``completed_units``/``total_units``
        count (cohort, variant) replays, and ``attempts`` above 1 means a
        previous attempt was interrupted and this one resumed it.

        A run whose heartbeat expired reports ``interrupted`` rather than
        pretending to still be working.
        """

        self._require_knowledge_base(knowledge_base)
        import pheasant.evaluation as evaluation

        payload = evaluation.progress(self.state, self.config.knowledge_base_id, run_id)
        payload["enabled"] = bool(self.config.evaluation.enabled)
        return payload

    # ---- the tuning plane ------------------------------------------------

    def start_retrieval_tuning(
        self,
        knowledge_base: str,
        diagnose_only: bool = False,
        apply: bool = False,
        force: bool = False,
    ) -> dict:
        """Start a retrieval tuning batch and return immediately with its id.

        Answers the question an agent cannot otherwise ask: *which step of
        retrieval is failing*. After the merge a lexical miss, a filtered-out
        document, a fusion demotion and a truncation all look identical -- an
        absent result -- and they call for four different responses.

        ``diagnose_only`` runs the first movement and stops: it attributes
        every miss to the stage that lost it and proposes nothing. That is the
        right call first, because the most valuable thing this can tell you is
        that the failures are *not* in a stage any retrieval parameter reaches.

        ``apply`` lets a winner that passes every gate -- including
        confirmation on a cohort it was never selected on -- become the
        region's live ranking for every replica. Off by default and worth
        leaving off: producing a bundle changes nothing, and applying one
        re-ranks the whole fleet.

        Minutes of work, so this hands back a job id. Poll
        :meth:`get_retrieval_tuning_status`; progress lives in ``/state``, so
        it survives a restart and is readable from any process.
        """

        self._require_knowledge_base(knowledge_base)
        import threading

        import pheasant.tuning as tuning

        if not self.config.tuning.enabled and not force:
            raise ValueError(
                "Retrieval tuning is disabled for this knowledge base. Enable "
                "`tuning.enabled` in pheasant.yaml, or pass force=true for a "
                "one-off batch."
            )
        in_flight = tuning.progress(self.state, self.config.knowledge_base_id)
        if in_flight.get("status") == "running":
            return {"status": "already-running", **in_flight}

        label = "tuning (diagnose)" if diagnose_only else "tuning"
        job = self.jobs.create("tuning", label, targets=["tuning"])

        def _run() -> None:
            try:
                kwargs: dict[str, Any] = {"force": True}
                if diagnose_only:
                    from pheasant.tuning.strategy import Budget

                    kwargs["budget"] = Budget(
                        refusion_trials=0, requery_trials=0, max_searches=10_000
                    )
                if apply:
                    self.config.tuning.auto.apply = True
                try:
                    outcome = tuning.run(
                        self.engine,
                        on_progress=lambda phase, detail: self.jobs.progress(
                            job.id, phase=phase, detail=detail or phase, source="tuning"
                        ),
                        **kwargs,
                    )
                finally:
                    if apply:
                        self.config.tuning.auto.apply = False
                self.jobs.finish(
                    job.id,
                    status="succeeded" if outcome.status != "failed" else "failed",
                    result={
                        "experiment_id": outcome.experiment_id,
                        "status": outcome.status,
                        "outcome": outcome.decision.outcome if outcome.decision else "",
                        "bundle_id": outcome.bundle_id,
                        "applied": outcome.applied,
                        "skipped_reason": outcome.skipped_reason,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - a job records its own failure
                self.jobs.finish(job.id, status="failed", error=str(exc))

        threading.Thread(target=_run, name="pheasant-mcp-tuning", daemon=True).start()
        return {"job_id": job.id, "status": "running", "diagnose_only": bool(diagnose_only)}

    def get_retrieval_tuning_status(
        self, knowledge_base: str, experiment_id: str | None = None
    ) -> dict:
        """How far a tuning batch has got: phase, units done, attempts, status.

        Read from ``/state``, so it answers for a batch this process did not
        start and for one whose process has since stopped. A batch whose
        heartbeat expired reports ``interrupted`` rather than pretending to
        still be working, and the next attempt resumes from its trials.
        """

        self._require_knowledge_base(knowledge_base)
        import pheasant.tuning as tuning

        payload = tuning.progress(self.state, self.config.knowledge_base_id, experiment_id)
        payload["enabled"] = bool(self.config.tuning.enabled)
        return payload

    def get_retrieval_diagnosis(
        self, knowledge_base: str, experiment_id: str | None = None
    ) -> dict:
        """Where retrieval is losing documents, by pipeline stage.

        The histogram, its denominator, and whether each stage is one a
        parameter can reach. A stage marked not-reachable is a statement that
        no amount of ranking work will help -- the failure is upstream, in what
        is indexed or how it is chunked -- and it is the most useful thing here.
        """

        self._require_knowledge_base(knowledge_base)
        from pheasant.tuning import store as tuning_store
        from pheasant.tuning.stages import ACTIONABLE_STAGES

        row = (
            tuning_store.experiment_status(self.state, experiment_id)
            if experiment_id
            else tuning_store.latest_experiment(self.state, self.config.knowledge_base_id)
        )
        diagnosis = (row or {}).get("diagnosis")
        if not diagnosis:
            raise ValueError(
                f"No retrieval diagnosis for {experiment_id}"
                if experiment_id
                else "No tuning batch has completed for this knowledge base yet. "
                "Run `start_retrieval_tuning` with diagnose_only=true."
            )
        histogram = diagnosis.get("histogram") or {}
        return {
            "experiment_id": (row or {}).get("experiment_id", ""),
            "summary": diagnosis.get("summary", ""),
            "histogram": histogram,
            "stages": [
                {
                    "stage": entry["stage"],
                    "count": entry["count"],
                    "reachable_by_tuning": entry["stage"] in ACTIONABLE_STAGES,
                }
                for entry in histogram.get("ranked") or []
            ],
        }

    def get_retrieval_parameters(self, knowledge_base: str) -> dict:
        """What this region ranks with, where it came from, and what is tunable.

        ``provenance`` is ``config`` or ``bundle``. A ranking nobody expects is
        most often a bundle somebody applied, and this is where that shows.
        """

        self._require_knowledge_base(knowledge_base)
        import pheasant.tuning as tuning
        from pheasant.tuning import glossary
        from pheasant.tuning import objective as objective_module
        from pheasant.tuning.bundle import as_config_fragment
        from pheasant.tuning.space import ParameterSpace

        active = tuning.active_parameters(self.state, self.config.knowledge_base_id, self.config)
        space = ParameterSpace(pinned=frozenset(self.config.tuning.pinned_parameters or ()))
        return {
            "active": active,
            "space": space.as_dict(),
            "config_fragment": as_config_fragment(active),
            "objective": objective_module.resolve(self.config.tuning.objective).as_dict(),
            "explanations": {entry["term"]: entry for entry in glossary.catalog()["parameters"]},
        }

    def list_tuning_bundles(self, knowledge_base: str, limit: int = 20) -> dict:
        """Configuration bundles this region has produced, and which is live."""

        self._require_knowledge_base(knowledge_base)
        from pheasant.tuning import store as tuning_store

        return {
            "bundles": tuning_store.list_bundles(
                self.state, self.config.knowledge_base_id, limit=max(1, min(int(limit), 200))
            )
        }

    def apply_tuning_bundle(
        self, knowledge_base: str, bundle_id: str, applied_by: str = "mcp"
    ) -> dict:
        """Make a bundle the fleet's live retrieval overlay.

        Fleet-scoped: one row, resolved by every replica reading this
        ``/state`` within its refresh window. There is deliberately no
        per-principal or per-request variant -- parameters that varied by
        caller would make two agents disagree about what this region contains.

        Reversible: :meth:`rollback_tuning_bundle` returns the region to its
        configured values, and what the bundle replaced is stored on it.
        """

        self._require_knowledge_base(knowledge_base)
        from pheasant.tuning import store as tuning_store

        payload = tuning_store.apply_bundle(
            self.state, self.config.knowledge_base_id, bundle_id, applied_by=applied_by or "mcp"
        )
        self._invalidate_ranking()
        return {"applied": True, "bundle": payload}

    def rollback_tuning_bundle(self, knowledge_base: str, to: str = "base") -> dict:
        """Stand the active overlay down, to the base or an earlier bundle.

        ``to`` defaults to ``"base"`` — the configured values, which are the
        thing an operator can read in a file. An earlier bundle id steps back
        to a decision somebody made before, and is recorded as a rollback
        rather than a fresh apply that happens to use old numbers.
        """

        self._require_knowledge_base(knowledge_base)
        from pheasant.tuning import store as tuning_store

        target = to or "base"
        if target != "base" and not tuning_store.load_bundle_row(
            self.state, self.config.knowledge_base_id, target
        ):
            raise ValueError(f"Unknown bundle: {target}")
        reverted = tuning_store.revert_bundle(
            self.state, self.config.knowledge_base_id, applied_by="mcp", to=target
        )
        self._invalidate_ranking()
        return {"reverted": reverted is not None, "bundle": reverted, "to": target}

    def get_retrieval_health(self, knowledge_base: str, since: str | None = None) -> dict:
        """How the pipeline is behaving on live traffic, between batches.

        Needs no cohort, no replay and no proof: it reads the sampled stage
        digests off the interaction ledger. Consequently it says what the
        pipeline *did* — empty rate and the stage that lost it, per-arm
        contribution, filter drops, truncation — and never whether an answer
        was correct.

        Use it as a change detector. An empty rate that moves after a bundle
        is applied is a fact about the bundle, and it is visible without
        waiting for the next batch.
        """

        self._require_knowledge_base(knowledge_base)
        from pheasant.tuning.health import stage_health

        payload = stage_health(self.state, self.config.knowledge_base_id, since=since)
        payload["sample_rate"] = self.config.observability.interactions.stage_sample_rate
        return payload

    def cancel_retrieval_tuning(
        self, knowledge_base: str, experiment_id: str | None = None
    ) -> dict:
        """Ask the running tuning batch to stand down at its next checkpoint.

        The batch stops with its trials already stored, so cancelling is
        resumable rather than destructive. It lands as a row, so it reaches a
        batch running in a different process or on a different replica.
        """

        self._require_knowledge_base(knowledge_base)
        import pheasant.tuning as tuning
        from pheasant.tuning import store as tuning_store

        target = experiment_id or (
            tuning.progress(self.state, self.config.knowledge_base_id) or {}
        ).get("experiment_id")
        if not target:
            raise ValueError("No tuning batch is running for this knowledge base.")
        if not tuning_store.request_cancel(
            self.state, self.config.knowledge_base_id, target, requested_by="mcp"
        ):
            raise ValueError(f"{target} is not running, so there is nothing to cancel.")
        return {"cancelled": True, "experiment_id": target}

    def pin_retrieval_parameters(self, knowledge_base: str, pinned: list[str]) -> dict:
        """Settle parameters so no tuning batch proposes them again.

        Replaces the whole list rather than adding to it, so a caller states
        the set they want rather than a delta against one they may not have
        read.
        """

        self._require_knowledge_base(knowledge_base)
        from pheasant.search.ranking import PARAMETER_STAGES

        unknown = sorted(set(pinned or []) - set(PARAMETER_STAGES))
        if unknown:
            raise ValueError(
                f"Not ranking parameters: {', '.join(unknown)}. "
                f"Known: {', '.join(sorted(PARAMETER_STAGES))}"
            )
        self.config.tuning.pinned_parameters = sorted(set(pinned or []))
        return {"pinned": self.config.tuning.pinned_parameters}

    def list_tuning_trials(
        self, knowledge_base: str, experiment_id: str | None = None, limit: int = 50
    ) -> dict:
        """Every trial in an experiment: point, score, stage, and why it was tried.

        The audit trail behind a decision, at the granularity an agent can
        reason about — each row names the parameter it moved, from what to
        what, the stage that motivated it, and whether it cost a real search.
        """

        self._require_knowledge_base(knowledge_base)
        from pheasant.tuning import store as tuning_store

        target = experiment_id or (
            tuning_store.latest_experiment(self.state, self.config.knowledge_base_id) or {}
        ).get("experiment_id")
        if not target:
            raise ValueError("No tuning batch has run for this knowledge base yet.")
        trials = tuning_store.load_trials(self.state, target)
        rows = sorted(
            (t for t in trials.values() if not t["failed"]),
            key=lambda t: -(t["metrics"].get(tuning_store.PRIMARY_METRIC) or 0.0),
        )
        return {
            "experiment_id": target,
            "primary_metric": tuning_store.PRIMARY_METRIC,
            "trials": rows[: max(1, min(int(limit), 500))],
        }

    def explain_retrieval_measures(self, knowledge_base: str, term: str | None = None) -> dict:
        """What every tuning measure means, and — more usefully — what it does not.

        Call this before acting on a number from `get_retrieval_health` or a
        tuning report. Each entry says what the measure is (with its
        denominator), what to do differently if it moves, and the misreading
        it invites: a truncation rate of 42% looks alarming and is normal, an
        empty rate of 0% is not evidence that results are good, and a metric
        reporting `insufficient_evidence` is not reporting zero.

        Pass ``term`` for one entry, or omit it for the whole catalog grouped
        into metrics, live-health rates, pipeline stages, gates and parameters.
        """

        self._require_knowledge_base(knowledge_base)
        from pheasant.tuning import glossary

        if term:
            entry = glossary.lookup(term)
            if entry is None:
                raise ValueError(
                    f"No explanation for {term!r}. Call this without a term for the full catalog."
                )
            return entry
        return glossary.catalog()

    def get_tuning_objective(self, knowledge_base: str) -> dict:
        """What "better" means here, and what optimizing it accepts getting worse.

        A region whose callers read one result should tune for reciprocal
        rank; one whose callers read a page and synthesize should tune for
        recall, and would be harmed by a parameter set that sharpens rank one
        at the cost of dropping a document out of the list. Both are
        legitimate and they are different objectives, so this reports which
        one is in force rather than assuming.
        """

        self._require_knowledge_base(knowledge_base)
        from pheasant.tuning import objective as objective_module

        return {
            "active": objective_module.resolve(self.config.tuning.objective).as_dict(),
            "available": objective_module.catalog(),
            "configured_by": "tuning.objective.metric, or tuning.objective.weights",
        }

    def get_retrieval_lineage(self, knowledge_base: str, limit: int = 50) -> dict:
        """Every configuration this region has served, newest first.

        What changed, when, who applied it, and what it replaced. This is the
        question asked after a regression, at exactly the moment the active
        configuration no longer holds the answer.
        """

        self._require_knowledge_base(knowledge_base)
        from pheasant.tuning import store as tuning_store

        return {
            "lineage": tuning_store.lineage(
                self.state, self.config.knowledge_base_id, limit=max(1, min(int(limit), 200))
            ),
            "base_explanation": (
                "Below the oldest entry is the configured base — "
                "`search.ranking` in the mounted pheasant.yaml. Rolling back "
                "returns there, or to any earlier entry."
            ),
        }

    def _invalidate_ranking(self) -> None:
        """Make this process pick up an overlay change at once.

        Other replicas converge on their own TTL, which is the point of a
        polled overlay; this is only about not making the process that changed
        it wait for its own poll.
        """

        resolver = getattr(getattr(self.searcher, "store", None), "ranking", None)
        invalidate = getattr(resolver, "invalidate", None)
        if callable(invalidate):
            invalidate()

    def get_evaluation_taxonomy(self, knowledge_base: str) -> dict:
        """Every evidence event type, its polarity and strength, and what it licenses."""

        self._require_knowledge_base(knowledge_base)
        from pheasant.evaluation.proof import DEFAULT_TAXONOMY

        return {
            "events": [
                {
                    "event_type": kind.event_type,
                    "polarity": str(kind.polarity),
                    "strength": str(kind.strength),
                    "note": kind.note,
                }
                for kind in DEFAULT_TAXONOMY.values()
            ],
            "defaults": {
                "unknown_is_negative": bool(self.config.evaluation.proof.unknown_is_negative),
                "non_selection_is_negative": bool(
                    self.config.evaluation.proof.non_selection_is_negative
                ),
            },
            "note": (
                "Exposure is not success and non-selection is not a negative. "
                "Both stay unknown unless this deployment has deliberately "
                "re-polarized them."
            ),
        }

    def describe_retrieval(self, knowledge_base: str) -> dict:
        """What this region's retrieval is configured to do, and what is tunable.

        An agent pointed at an unfamiliar region cannot otherwise tell whether
        semantic search exists, how many rounds the answering loop runs, or
        which sources are even present — so it either guesses or asks for
        everything. This returns the standing configuration, the criteria that
        may be overridden per call, and what the region can actually do
        (a vector mode is not offered when no vector index is built).
        """
        self._require_knowledge_base(knowledge_base)
        from pheasant.services.assistant import RETRIEVAL_FIELD_HELP

        settings = self.config.assistant.retrieval
        has_vectors = self.engine.vectors is not None
        modes = ["text", "graph", "hybrid"] + (["vector"] if has_vectors else [])
        rows = self.state.rows("SELECT DISTINCT source_id FROM artifacts ORDER BY source_id")
        # What kinds of source this region actually holds, so an agent can pick
        # a `source_types` filter without first listing every source and
        # inspecting each one. Read from the registry, not from config, so a
        # source registered at runtime is included.
        from pheasant.search.criteria import source_type_map

        types = source_type_map(self.state)
        indexed = [str(row["source_id"]) for row in rows]
        source_types = sorted({types[name] for name in indexed if name in types})
        return {
            "knowledge_base": knowledge_base or self.config.knowledge_base_id,
            "default_mode": self.config.search.default_mode,
            "max_results_default": self.config.search.max_results_default,
            "available_modes": modes,
            "vector_index": {
                "enabled": self.config.search.embeddings.enabled,
                "built": has_vectors,
                "provider": self.config.search.embeddings.provider,
            },
            "sources": indexed,
            "source_types": source_types,
            "memory": self._describe_memory(),
            "retrieval": settings.model_dump(mode="json"),
            "workflow": self.config.assistant.workflow,
            "workflow_options": dict(self.config.assistant.workflow_options or {}),
            "tunable_per_call": {
                "search_context": [
                    "mode",
                    "max_results",
                    "source_name",
                    "exclude_sources",
                    "source_types",
                    "exclude_source_types",
                    "node_types",
                    "min_score",
                    "memory",
                ],
                "ask_knowledge_base": ["workflow", "mode", "max_results", "options"],
            },
            "field_help": RETRIEVAL_FIELD_HELP,
        }

    def _describe_steering(self) -> dict:
        """The rules that actually re-rank this region's searches (Step 33.8).

        Reported because steering is invisible otherwise: a query that lands
        differently because someone once wrote down a synonym is a mystery
        unless the region can say so.
        """
        if not self.config.memory.steering_enabled:
            return {"enabled": False}
        from pheasant.memory.policy import MemoryPolicy, utc_now_iso
        from pheasant.memory.steering import load_steering, load_steering_records

        rules = load_steering(
            load_steering_records(self.state),
            MemoryPolicy(),
            now=utc_now_iso(),
            enabled=True,
        )
        return {"enabled": True, **rules.describe()}

    def _memory_graph_coverage(self) -> dict:
        """How many records are actually wired into the graph (Step 33.7).

        Computed from the live graph rather than from a stored report, so it
        cannot go stale: "12 records, 9 bridged" is the difference between a
        bridge that is working and one that silently connects nothing, and an
        operator has no other way to tell those apart.
        """
        from pheasant.memory.bridge import ABOUT_EDGE

        try:
            rows = self.state.rows("SELECT artifact_id FROM memory_records")
        except Exception:  # pragma: no cover - state store older than 33.5
            return {}
        artifacts = {str(row["artifact_id"]) for row in rows}
        if not artifacts:
            return {"records": 0, "bridged": 0, "unbridged": 0, "by_signal": {}}

        bridged: set[str] = set()
        by_signal: dict[str, int] = {}
        graph = self.graph
        remote = getattr(graph, "remote_memory_coverage", None)
        if callable(remote):
            return remote(artifact_ids=sorted(artifacts))
        with graph.reading():
            for (source, _target), edge_map in graph.iter_edges():
                if source not in artifacts:
                    continue
                for data in edge_map.values():
                    if data.get("type") == ABOUT_EDGE:
                        bridged.add(source)
                        signal = str(data.get("match_signal") or "unknown")
                        by_signal[signal] = by_signal.get(signal, 0) + 1
        return {
            "records": len(artifacts),
            "bridged": len(bridged),
            "unbridged": len(artifacts) - len(bridged),
            "by_signal": by_signal,
        }

    def _describe_memory(self) -> dict:
        """What this region remembers, and how a caller may ask for it.

        Without this an agent could only turn memory off by naming the source,
        which it had no way to learn: `sources` is a list of bare ids and
        nothing said which one was the memory. Reporting the scopes and counts
        that actually exist is also the difference between "this region has
        memory" and "this region has memory *about deployments, written this
        week*", which is what decides whether to ask for it at all.
        """
        from pheasant.memory.policy import VALID_MODES
        from pheasant.memory.store import memory_source

        source = memory_source(self.config, self.state)
        if source is None:
            return {"enabled": False}
        try:
            rows = self.state.rows(
                "SELECT scope, COUNT(*) AS n, "
                "SUM(CASE WHEN valid_until IS NULL THEN 1 ELSE 0 END) AS current "
                "FROM memory_records GROUP BY scope ORDER BY scope"
            )
        except Exception:  # pragma: no cover - state store older than 33.5
            rows = []
        return {
            "enabled": True,
            "source_name": source.name,
            "graph": self._memory_graph_coverage(),
            "scopes": {
                str(row["scope"]): {
                    "records": int(row["n"] or 0),
                    "current": int(row["current"] or 0),
                }
                for row in rows
            },
            "modes": list(VALID_MODES),
            "default_mode": self.config.memory.default_policy,
            "steering": self._describe_steering(),
            "accepts": ["mode", "scopes", "subject", "current_only", "as_of", "max_results"],
            "note": (
                "Corrected records are excluded by default; pass "
                "{'current_only': false} or an 'as_of' instant to see them."
            ),
        }

    def preview_retrieval(
        self,
        knowledge_base: str,
        query: str,
        mode: str = "hybrid",
        max_results: int = 10,
        source_name: str | None = None,
        exclude_sources: list[str] | None = None,
        node_types: list[str] | None = None,
        min_score: float | None = None,
        memory: Any = None,
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
    ) -> dict:
        """Run retrieval criteria and report how they differ from the config.

        The point is to make a configuration **testable before it is
        committed**: the same criteria that would go into
        ``assistant.retrieval`` / ``search.default_mode`` can be tried here,
        against the region's own content, and compared with what the standing
        configuration returns for the same query. Returns both result sets in
        compact form plus the delta — what the criteria added, dropped and
        kept — so an agent can decide rather than guess.

        Read-only: nothing is persisted and the live config is untouched.
        """
        self._require_knowledge_base(knowledge_base)
        kb = knowledge_base or self.config.knowledge_base_id

        baseline = self.searcher.search_context(
            kb,
            query,
            self.config.search.default_mode,
            self.config.search.max_results_default,
            graph=self.graph,
            security=self.config.security,
        )
        candidate = self.search_context(
            kb,
            query,
            mode,
            max_results,
            source_name=source_name,
            exclude_sources=exclude_sources,
            node_types=node_types,
            min_score=min_score,
            memory=memory,
            source_types=source_types,
            exclude_source_types=exclude_source_types,
        )

        def keys(payload: dict) -> list[str]:
            out = []
            for item in payload.get("results") or []:
                out.append(str(item.get("id") or item.get("node_id") or item.get("path") or ""))
            return [key for key in out if key]

        baseline_keys = keys(baseline)
        candidate_keys = keys(candidate)
        baseline_set, candidate_set = set(baseline_keys), set(candidate_keys)
        return {
            "query": query,
            "criteria": {
                "mode": mode,
                "max_results": max_results,
                "source_name": source_name,
                "exclude_sources": exclude_sources,
                "node_types": node_types,
                "min_score": min_score,
                "memory": memory,
            },
            "configured": {
                "mode": self.config.search.default_mode,
                "max_results": self.config.search.max_results_default,
            },
            "results": _preview_rows(candidate.get("results") or []),
            "baseline_results": _preview_rows(baseline.get("results") or []),
            "delta": {
                "added": [key for key in candidate_keys if key not in baseline_set],
                "dropped": [key for key in baseline_keys if key not in candidate_set],
                "kept": [key for key in candidate_keys if key in baseline_set],
                "result_count": len(candidate_keys),
                "baseline_count": len(baseline_keys),
            },
            "persisted": False,
            "how_to_apply": (
                "PUT /assistant/retrieval, or set search.default_mode / "
                "assistant.retrieval in pheasant.yaml (`pheasant setup --advanced`)."
            ),
        }

    def ask_knowledge_base(
        self,
        knowledge_base: str,
        question: str,
        workflow: str | None = None,
        mode: str = "hybrid",
        max_results: int = 8,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        options: dict | None = None,
        source_types: list[str] | None = None,
        exclude_source_types: list[str] | None = None,
    ) -> dict:
        """Answer a question from the knowledge base, with citations and graph facts.

        Runs the configured question-answering workflow — by default the
        LangGraph agent when the ``[agent]`` extra is installed and a model
        is reachable, otherwise a single retrieval pass. Prefer this over
        ``search_context`` when you want a synthesized answer rather than
        raw passages to reason over yourself; the returned ``steps`` show
        what the agent actually did.

        With no model configured the answer is extractive (the retrieved
        passages, attributed), so this is always safe to call.
        """
        from pheasant.assistant.chat import answer_question

        self._require_knowledge_base(knowledge_base)
        return answer_question(
            question,
            search=self.searcher,
            knowledge_base=knowledge_base or self.config.knowledge_base_id,
            config=self.config,
            graph=self.graph,
            state=self.state,
            mode=mode,
            max_results=max_results,
            source_name=source_name,
            principal=principal,
            principal_groups=principal_groups,
            workflow=workflow,
            options=options,
            source_types=source_types,
            exclude_source_types=exclude_source_types,
        )

    def get_relevant_files(
        self,
        knowledge_base: str,
        task: str,
        source_name: str | None = None,
        max_files: int = 8,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
        section: str | None = None,
        memory: Any = None,
    ) -> dict:
        """Files most relevant to a described task.

        Transport adapter for `services.retrieval.relevant_files`. That shared
        implementation is what makes three behaviours true here that were true
        only over HTTP before: results are deduplicated to one entry per file,
        `section` narrows to a document's taxonomy, and the memory policy
        applies — so a corrected record is no longer served to an agent.

        ``section`` and ``memory`` are new parameters with defaults matching
        the previous behaviour where it was correct to keep it, which is
        additive evolution (rule 8).
        """

        self._require_knowledge_base(knowledge_base)
        return retrieval_service.relevant_files(
            self.services,
            retrieval_service.FilesRequest(
                task=task,
                knowledge_base=knowledge_base,
                max_files=max_files,
                source_name=source_name,
                section=section,
                principal=principal,
                principal_groups=principal_groups,
                memory=memory,
            ),
        )

    def get_graph_neighbors(
        self,
        knowledge_base: str,
        node_id: str,
        depth: int = 1,
        edge_types: list[str] | None = None,
        max_nodes: int | None = None,
    ) -> dict:
        """Neighbouring nodes, breadth-first.

        Transport adapter for `services.graph.neighbors`. This used to be a
        second BFS — without the hierarchy-first ordering, without `max_nodes`
        and without either exclusion — so the same node returned a different
        set of neighbours in a different order depending on which surface asked.
        It also imported the HTTP module for the remote-graph case, which put
        `api.app` on the import path of the tool layer.

        ``max_nodes`` is new here and defaults to unbounded, which is what this
        tool did before (rule 8: additive).
        """

        self._require_knowledge_base(knowledge_base)
        return graph_service.neighbors(self.graph, node_id, depth, edge_types, max_nodes=max_nodes)

    def get_file_summary(
        self,
        knowledge_base: str,
        path: str,
        source_name: str | None = None,
        principal: str | None = None,
        principal_groups: list[str] | None = None,
    ) -> dict:
        """One indexed file: its artifact row, ordered summary, and content.

        Transport adapter for `services.graph.file_summary`. Two things the
        shared implementation brings that this tool did not have: chunk-ordered
        concatenation (a bare `GROUP_CONCAT` returned summaries in whatever
        order the join produced) and the **ACL check** — this endpoint hands
        back an artifact body, and the guard existed on the HTTP route only.
        """

        self._require_knowledge_base(knowledge_base)
        return graph_service.file_summary(
            self.services, path, source_name, principal, principal_groups
        )

    def get_repo_map(self, knowledge_base: str, source_name: str, depth: int = 3) -> dict:
        """Every indexed path in one source. Adapter for `services.graph.repo_map`."""

        self._require_knowledge_base(knowledge_base)
        return graph_service.repo_map(self.services, source_name, depth)

    def explain_node(self, knowledge_base: str, node_id: str) -> dict:
        """What a node is. Adapter for `services.graph.explain_node`.

        The shared implementation returns the node's attributes under `node`,
        which this tool omitted and the HTTP route did not.
        """

        self._require_knowledge_base(knowledge_base)
        return graph_service.explain_node(self.services, node_id)

    def get_sync_status(self, knowledge_base: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        return {
            "sources": SourceRegistry(self.config, self.state).list_sources(),
            "checkpoints": self.state.list_source_checkpoints(),
        }

    def get_source_manifest(self, knowledge_base: str, source_name: str) -> dict:
        self._require_knowledge_base(knowledge_base)
        self._require_source(source_name)
        return self.engine.manifests.load(source_name)

    def get_contract(self, knowledge_base: str) -> dict:
        """Return this region's published semantic contract (Synapse 21.5).

        ``{"status": "unpublished"}`` until a contract has been published.
        """
        self._require_knowledge_base(knowledge_base)
        import json as _json

        from pheasant.synapse.publisher import load_contract_text

        text = load_contract_text(self.config.pheasant.state_path)
        if text is None:
            return {"status": "unpublished", "knowledge_base": self.config.knowledge_base_id}
        return _json.loads(text)

    def get_sync_history(
        self,
        knowledge_base: str,
        source_name: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        self._require_knowledge_base(knowledge_base)
        return {
            "events": self.state.list_source_audit_events(source_name, limit, offset),
            "pagination": {"limit": limit, "offset": offset},
        }

    def get_graph_slice(
        self,
        knowledge_base: str,
        node_id: str,
        depth: int = 1,
        edge_types: list[str] | None = None,
        limit: int = 100,
    ) -> dict:
        """A bounded sub-graph around a node.

        Transport adapter for `services.graph.slice_`. The third
        implementation of this walk lived here and was the thinnest: it kept no
        `depths` map, reported no `truncated` flag, and asked for exactly
        `limit` neighbours rather than one more — so a slice that had filled
        its budget was indistinguishable from one that had run out of graph.
        """

        self._require_knowledge_base(knowledge_base)
        return graph_service.slice_(self.graph, node_id, depth, edge_types, limit)

    def _require_knowledge_base(self, knowledge_base: str | None) -> None:
        """Refuse a knowledge base this region does not hold.

        Through the shared vocabulary since the service extraction, so the two
        surfaces say the same thing: `UnknownKnowledgeBase` subclasses
        `ValueError`, which is what `server.py` already translates into an
        informative `ToolError`, so the SDK boundary is unchanged.
        """

        self.services.knowledge_base(knowledge_base)

    def _require_source(self, source_name: str) -> None:
        if not self.state.get_source(source_name):
            raise SourceNotFound(source_name)

    def _source_config(self, source_name: str) -> SourceConfig:
        for source in self.config.sources:
            if source.name == source_name:
                return source
        row = self.state.get_source(source_name)
        if not row:
            raise KeyError(f"Unknown source: {source_name}")
        payload = json.loads(row["config_json"])
        return PheasantConfig.model_validate({"sources": [payload]}).sources[0]

    def _audit(
        self,
        source_id: str | None,
        action: str,
        actor: str | None,
        transport: str | None,
        client_id: str | None,
        created_at: str,
        details: dict | None = None,
    ) -> None:
        digest = hashlib.sha256(
            json.dumps(
                {
                    "source_id": source_id,
                    "action": action,
                    "created_at": created_at,
                    "details": details or {},
                },
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
        ordinal = len(self.state.list_source_audit_events(source_id, limit=10000))
        self.state.append_source_audit_event(
            f"audit:{digest}:{ordinal}",
            source_id,
            action,
            actor,
            transport,
            client_id,
            created_at,
            details,
        )
