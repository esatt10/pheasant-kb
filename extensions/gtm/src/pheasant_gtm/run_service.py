"""Application operations shared by the GTM research CLI and HTTP API."""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

from pheasant.security.identity import current_identity
from pheasant.services import ServiceContext, models
from pheasant.services.errors import InvalidRequest, ServiceError
from pheasant_gtm import presentation, workflow
from pheasant_gtm.catalog import FieldSpec, Request, RunControl, now_iso
from pheasant_gtm.evidence import eligible, resolve, verify_proposals
from pheasant_gtm.run_store import RunStore
from pheasant_gtm.runtime import IsolationUnavailable


class UnknownRun(LookupError):
    pass


class BusyRun(RuntimeError):
    pass


class LegacyRun(RuntimeError):
    """This checkpoint predates the isolated parser."""


class RunNotFound(ServiceError):
    status = 404
    code = "GTM_RUN_NOT_FOUND"


class RunBusy(ServiceError):
    status = 409
    code = "GTM_RUN_BUSY"
    retryable = True


class RunUpgradeRequired(ServiceError):
    status = 409
    code = "GTM_RUN_UPGRADE_REQUIRED"


class ParserUnavailable(ServiceError):
    status = 503
    code = "GTM_PARSER_UNAVAILABLE"
    retryable = True


def operation(context: ServiceContext, kb: str | None, action, *args, **kwargs):
    identity = current_identity()
    store = RunStore(
        context.state, context.knowledge_base(kb), identity.principal if identity else ""
    )
    try:
        return action(store, *args, **kwargs)
    except UnknownRun as exc:
        raise RunNotFound(str(exc)) from exc
    except BusyRun as exc:
        raise RunBusy(str(exc)) from exc
    except LegacyRun as exc:
        raise RunUpgradeRequired(str(exc)) from exc
    except IsolationUnavailable as exc:
        raise ParserUnavailable(str(exc)) from exc
    except ServiceError:
        raise
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc


def _require_current(run: dict) -> None:
    if run.get("version") != workflow.CHECKPOINT_VERSION:
        raise LegacyRun("This checkpoint predates GTM isolation. Start a new run for the company.")


def start(store: RunStore, request: Request, *, config: Any = None) -> dict:
    if config is not None and (request.model or request.decision_model):
        models.resolve(
            models.effective_config(config, store.state), request.model, request.decision_model
        )
    payload = workflow.initialize(request)
    return status(store, store.create(payload))


def _read(store: RunStore, run_id: str) -> tuple[dict, dict]:
    row = store.get(run_id)
    if not row:
        raise UnknownRun("unknown GTM research run")
    return row, json.loads(row["payload_json"])


def status(store: RunStore, run_id: str) -> dict:
    row, run = _read(store, run_id)
    if run.get("version") != workflow.CHECKPOINT_VERSION:
        return {"run_id": run_id, "phase": "security_upgrade_required"}
    phase = run["phase"]
    return {
        "run_id": run_id,
        "phase": phase,
        "paused": run.get("paused", False),
        "next_step_at": run.get("next_step_at"),
        "acquisition_policy": Request.model_validate(run["request"]).acquisition.model_dump(),
        "acquisition": presentation.acquisition(run),
        "research": run.get("research"),
        "token_usage": presentation.token_usage(run),
        "as_of": run["as_of"],
        "identity": run["identity"]["status"],
        "busy": bool(row["lease_until"] and row["lease_until"] > now_iso()),
        "lease_until": row["lease_until"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "page_requests": sum(task["attempts"] for task in run["tasks"]),
        "collection_requests": sum(
            task["attempts"] for task in run["tasks"] if task["role"] not in {"anchor", "candidate"}
        ),
        "budgeted_page_attempts": workflow.collection_attempts(run),
        "page_limit": run["request"]["page_limit"],
        "excerpt_limit": run["request"].get("excerpt_limit"),
        "page_tasks_pending": sum(not task.get("done", False) for task in run["tasks"])
        if phase in {"identity", "collect"}
        else 0,
        "documents": dict(
            Counter(doc["kind"] for doc in run["documents"].values() if doc["attributed"])
        ),
        "errors": len(run["errors"]),
        "last_issue_stage": run["errors"][-1]["stage"] if run["errors"] else None,
        "quarantined": sum(bool(doc.get("quarantined")) for doc in run["documents"].values()),
        "review_batches": {"completed": run["reviewed"], "total": len(run["review_batches"])},
        "decision_calls": run["decision_calls"],
        "search_queries_run": sum(bool(query.get("done")) for query in run["queries"]),
    }


def report(store: RunStore, run_id: str, *, evidence: bool = False) -> dict:
    row, run = _read(store, run_id)
    _require_current(run)
    profile = workflow.profile(run)
    packet = {
        "run_id": run_id,
        "phase": run["phase"],
        "acquisition": presentation.acquisition(run),
        "token_usage": presentation.token_usage(run),
        **(profile if evidence else presentation.summary(profile)),
    }
    if evidence:
        packet.update(
            persisted=presentation.storage_manifest(run, len(row["payload_json"].encode())),
            page_tasks=run["tasks"],
            parsed_documents=list(run["documents"].values()),
        )
    return packet


def persistence(store: RunStore, run_id: str) -> dict:
    row, run = _read(store, run_id)
    _require_current(run)
    return {
        "run_id": run_id,
        **presentation.storage_manifest(run, len(row["payload_json"].encode())),
    }


def _change(store: RunStore, run_id: str, operation) -> None:
    _read(store, run_id)
    lease = store.claim(run_id)
    if lease is None:
        raise BusyRun("another worker holds this run's checkpoint lease")
    token, payload = lease
    try:
        _require_current(payload)
        operation(payload)
    except BaseException:
        store.commit(run_id, token, None)
        raise
    if not store.commit(run_id, token, payload):
        raise BusyRun("checkpoint lease expired and another worker took over")


def _advance(store: RunStore, run: dict, config: Any) -> None:
    if (
        config is None
        or run["phase"] not in {"extract", "research", "review"}
        or not run["request"].get("review")
    ):
        workflow.advance(run)
        return

    from pheasant.assistant.answering import resolve_llm
    from pheasant.assistant.decisions import resolve_decider
    from pheasant.connections import environment as connection_environment

    request = Request.model_validate(run["request"])
    try:
        chosen = models.config_for(
            models.effective_config(config, store.state), request.model, request.decision_model
        )
    except (models.UnknownModel, models.ModelSelectionDisabled):
        # A catalog change between checkpoints must not send evidence to a
        # different model from the one the caller explicitly selected.
        workflow.advance(run)
        return
    env = connection_environment()
    llm = resolve_llm(chosen, env=env)
    decider = (
        resolve_decider(chosen.assistant, llm, env)
        if llm is not None and request.decisions and run["phase"] == "review"
        else None
    )
    deadline = time.monotonic() + 60.0
    workflow.advance(
        run,
        llm=llm.with_deadline(deadline) if llm is not None else None,
        decider=decider.with_deadline(deadline) if decider is not None else None,
    )


def step(store: RunStore, run_id: str, *, config: Any = None) -> dict:
    current = status(store, run_id)
    if (
        current["phase"] == "complete"
        or current.get("paused")
        or ((current.get("next_step_at") or 0) > time.time())
    ):
        return current
    _change(store, run_id, lambda run: _advance(store, run, config))
    return status(store, run_id)


def control(store: RunStore, run_id: str, update: RunControl) -> dict:
    _change(store, run_id, lambda run: workflow.control(run, update))
    return status(store, run_id)


def _review_fields(run: dict) -> list[FieldSpec]:
    fields = [FieldSpec.model_validate(data) for data in run["catalog"]]
    results = resolve(fields, run["candidates"], run["as_of"])
    return [
        field
        for field in fields
        if not field.derive_from
        and not eligible(field, results)
        and field.key not in {"name", "domain", "website", "legal_name"}
    ]


def review_packet(store: RunStore, run_id: str, *, evidence: bool = False) -> dict:
    _, run = _read(store, run_id)
    _require_current(run)
    if run["phase"] != "complete":
        raise BusyRun("finish collection and extraction before IDE review")
    fields = _review_fields(run)
    selected = workflow.selections(run, fields)
    if not evidence:
        return {
            "run_id": run_id,
            "as_of": run["as_of"],
            "passages": presentation.passage_index(selected),
        }
    return {
        "company": run["company"],
        "website": run["website"],
        "as_of": run["as_of"],
        "fields": {
            field.key: field.model_dump(mode="json") for field in fields if field.key in selected
        },
        "passages": selected,
    }


def apply_review(store: RunStore, run_id: str, observations: list[dict[str, Any]]) -> dict:
    counts = {}

    def apply(run: dict) -> None:
        if run["phase"] != "complete":
            raise BusyRun("finish collection and extraction before IDE review")
        fields = _review_fields(run)
        accepted = verify_proposals(
            observations, workflow.docs(run), fields, workflow.selections(run, fields), "ide"
        )
        known = {json.dumps(item, sort_keys=True) for item in run["candidates"]}
        new = []
        for item in accepted:
            marker = json.dumps(item, sort_keys=True)
            if marker not in known:
                new.append(item)
                known.add(marker)
        counts.update(
            accepted=len(new),
            rejected=len(observations) - len(accepted),
            already_present=len(accepted) - len(new),
        )
        if len(run["candidates"]) + len(new) > 6500:
            raise ValueError("run has reached the maximum number of saved observations")
        run["candidates"].extend(new)

    _change(store, run_id, apply)
    return counts
