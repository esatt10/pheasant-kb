#!/usr/bin/env python3
"""Measure completed assistant answers over HTTP, SSE, or streamable HTTP MCP.

SSE also records first provisional answer text separately from workflow
progress and verified completion. Neither earlier event substitutes for the
completed-answer metric.

Case files are JSON arrays/objects or JSONL. A case has a stable ``id``, an
independently assigned ``complexity`` (moderate/complex/difficult/negative), a
``question``, and optional ``depth``, ``answerable``, ``expected_facts``,
``acceptable_passage_ids`` and ``negative_reason`` labels. The automated
quality values are lexical checks for triage, not a substitute for reviewing
claim support against the labeled evidence.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import random
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_THREAD = threading.local()
_MCP_PROTOCOL = "2025-03-26"


def _headers(token: str, *, mcp: bool = False) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if mcp:
        headers["Accept"] = "application/json, text/event-stream"
        headers["Content-Type"] = "application/json"
    return headers


def _post_json(url: str, data: dict[str, Any], headers: dict[str, str], timeout: float):
    request = urllib.request.Request(
        url,
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout)


def _parse_json_or_sse(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", "replace").strip()
    if text.startswith("data:") or "\ndata:" in text:
        candidates = [
            line[5:].strip()
            for line in text.splitlines()
            if line.startswith("data:") and line[5:].strip() not in {"[DONE]", ""}
        ]
        for candidate in reversed(candidates):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"_transport_error": "response was neither JSON nor MCP event data"}
    return (
        parsed
        if isinstance(parsed, dict)
        else {"_transport_error": "response JSON was not an object"}
    )


class McpSession:
    """One persistent MCP client session per benchmark worker thread."""

    def __init__(self, base_url: str, token: str, timeout: float):
        self.token = token
        self.timeout = timeout
        parsed = urllib.parse.urlsplit(base_url)
        path = parsed.path.rstrip("/")
        if path.endswith("/mcp"):
            mcp_path = path + "/"
        else:
            mcp_path = path + "/mcp/"
        self.url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, mcp_path, "", ""))
        self.session_id: str | None = None
        self.next_id = 1
        started = time.perf_counter()
        response = self._request(
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": _MCP_PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "pheasant-latency-benchmark", "version": "1"},
                },
            },
            initialized=False,
        )
        if response.get("error"):
            raise RuntimeError(f"MCP initialize failed: {response['error']}")
        self.setup_ms = (time.perf_counter() - started) * 1000
        self._notify_initialized()

    def _id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value

    def _request(self, message: dict[str, Any], *, initialized: bool = True) -> dict[str, Any]:
        headers = _headers(self.token, mcp=True)
        headers["MCP-Protocol-Version"] = _MCP_PROTOCOL
        if initialized and self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        with _post_json(self.url, message, headers, self.timeout) as response:
            self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
            return _parse_json_or_sse(response.read())

    def _notify_initialized(self) -> None:
        message = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        headers = _headers(self.token, mcp=True)
        headers["MCP-Protocol-Version"] = _MCP_PROTOCOL
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.url,
            data=json.dumps(message).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                pass
        except urllib.error.HTTPError as exc:
            # Some stateless transports do not need an explicit notification.
            if exc.code not in {202, 204, 400, 404, 405}:
                raise

    def call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "tools/call",
                "params": {"name": "ask_knowledge_base", "arguments": arguments},
            }
        )


def _mcp_session(base_url: str, token: str, timeout: float) -> tuple[McpSession, float | None]:
    key = (base_url, timeout)
    current = getattr(_THREAD, "mcp", None)
    if current is not None and current[0] == key:
        return current[1], None
    client = McpSession(base_url, token, timeout)
    _THREAD.mcp = (key, client)
    return client, client.setup_ms


def _answer_payload(outer: dict[str, Any]) -> dict[str, Any]:
    """Unwrap HTTP JSON or MCP structured/text content into the answer object."""
    if isinstance(outer.get("answer"), dict):
        return outer["answer"]
    result = outer.get("result")
    if not isinstance(result, dict):
        return outer
    for key in ("structuredContent", "structured_content"):
        value = result.get(key)
        if isinstance(value, dict):
            return value
    for item in result.get("content") or []:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            value = json.loads(item.get("text") or "")
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return outer


def _chat_request(case: dict[str, Any]) -> dict[str, Any]:
    request = {
        "question": case["question"],
        "mode": case.get("mode", "hybrid"),
        "depth": case.get("depth"),
        "workflow": case.get("workflow"),
        "max_results": case.get("max_results"),
        "source_name": case.get("source_name"),
        "principal": case.get("principal"),
        "principal_groups": case.get("principal_groups"),
        "source_types": case.get("source_types"),
        "exclude_source_types": case.get("exclude_source_types"),
        "options": case.get("options"),
        "memory": case.get("memory"),
        "history": case.get("history"),
        "visual": case.get("visual"),
    }
    return {key: value for key, value in request.items() if value is not None}


def _request_once(
    *,
    base_url: str,
    token: str,
    timeout: float,
    transport: str,
    case: dict[str, Any],
) -> dict[str, Any]:
    mcp_client = None
    setup_ms = None
    if transport == "mcp":
        mcp_client, setup_ms = _mcp_session(base_url, token, timeout)

    started = time.perf_counter()
    first_progress_ms = None
    first_answer_text_ms = None
    answer: dict[str, Any] = {}
    status: int | None = None
    error: str | None = None
    completed_at: float | None = None
    visual_completed_ms: float | None = None
    try:
        if transport == "http":
            url = base_url.rstrip("/") + "/assistant/chat"
            with _post_json(url, _chat_request(case), _headers(token), timeout) as response:
                status = response.status
                outer = _parse_json_or_sse(response.read())
            answer = _answer_payload(outer)
            if isinstance(answer.get("answer"), str) and answer["answer"].strip():
                completed_at = time.perf_counter()
            else:
                error = "HTTP response did not contain a completed answer"
        elif transport == "sse":
            url = base_url.rstrip("/") + "/assistant/chat/stream"
            with _post_json(
                url,
                _chat_request(case),
                {**_headers(token), "Accept": "text/event-stream"},
                timeout,
            ) as response:
                status = response.status
                for raw_line in response:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    event_type = event.get("type")
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    if event_type == "step" and first_progress_ms is None:
                        first_progress_ms = elapsed_ms
                    elif event_type == "draft" and first_answer_text_ms is None:
                        if str(event.get("delta") or "").strip():
                            first_answer_text_ms = elapsed_ms
                    elif event_type == "answer":
                        candidate = (
                            event.get("answer") if isinstance(event.get("answer"), dict) else {}
                        )
                        if isinstance(candidate.get("answer"), str) and candidate["answer"].strip():
                            answer = candidate
                            completed_at = time.perf_counter()
                        else:
                            error = "SSE answer event did not contain completed answer text"
                    elif event_type == "visual":
                        visual_completed_ms = elapsed_ms
                    elif event_type == "error":
                        error = str(event.get("error") or "stream returned an error")
            if completed_at is None and error is None:
                error = "SSE stream ended without a completed answer event"
        else:
            assert mcp_client is not None
            arguments = {
                "knowledge_base": case.get("knowledge_base") or "pheasant-lab",
                "question": case["question"],
                "mode": case.get("mode", "hybrid"),
                "max_results": case.get("max_results", 8),
            }
            for key in (
                "workflow",
                "source_name",
                "principal",
                "principal_groups",
                "options",
                "source_types",
                "exclude_source_types",
                "memory",
                "history",
                "depth",
                "visual",
            ):
                if case.get(key) is not None:
                    arguments[key] = case[key]
            outer = mcp_client.call(arguments)
            if outer.get("error"):
                error = str(outer["error"].get("message") or outer["error"])
            result = outer.get("result") or {}
            if result.get("isError"):
                error = error or "MCP tool returned isError"
            answer = _answer_payload(outer)
            status = 200 if error is None else 500
            if error is None and isinstance(answer.get("answer"), str) and answer["answer"].strip():
                completed_at = time.perf_counter()
            elif error is None:
                error = "MCP tool response did not contain a completed answer"
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", "replace")[:2000]
        error = f"HTTP {exc.code}: {body}"
    except TimeoutError as exc:
        error = f"timeout: {exc}"
    except urllib.error.URLError as exc:
        error = f"connection error: {exc.reason}"
    except Exception as exc:  # Each request is reported; one failure does not hide the run.
        error = f"{type(exc).__name__}: {exc}"

    ended = time.perf_counter()
    client_completed_ms = (completed_at - started) * 1000 if completed_at is not None else None
    return {
        "case_id": case.get("id") or case.get("case_id"),
        "complexity": case.get("complexity", "unknown"),
        "requested_answer_length": case.get("answer_length", case.get("depth", "unknown")),
        "transport": transport,
        "http_status": status,
        "client_completed_answer_ms": client_completed_ms,
        "client_request_end_ms": (ended - started) * 1000,
        "first_progress_ms": first_progress_ms,
        "first_progress_semantics": "first SSE workflow progress event; not first answer token",
        "first_answer_text_ms": first_answer_text_ms,
        "first_answer_text_semantics": "first provisional SSE answer text; final citations pending",
        "visual_completed_ms": visual_completed_ms,
        "mcp_session_setup_ms": setup_ms,
        "error": error,
        "admission_rejected": status == 429 or (answer.get("code") == "ASSISTANT_BUSY"),
        "timeout": bool(error and error.lower().startswith("timeout:")),
        "case_labels": {
            "answerable": case.get("answerable", True),
            "expected_facts": case.get("expected_facts") or [],
            "acceptable_passage_ids": case.get("acceptable_passage_ids") or [],
            "negative_reason": case.get("negative_reason"),
        },
        "answer": answer.get("answer"),
        "workflow": answer.get("workflow"),
        "answer_mode": answer.get("answer_mode", answer.get("mode")),
        "model": answer.get("model"),
        "reasoning_effort_requested": answer.get("reasoning_effort_requested"),
        "reasoning_effort_effective": answer.get("reasoning_effort_effective"),
        "reasoning_effort_by_stage": answer.get("reasoning_effort"),
        "retrieval_rounds": (answer.get("counts") or {}).get("rounds"),
        "model_call_rounds": (answer.get("counts") or {}).get("model_call_rounds"),
        "server_processing_ms": answer.get("server_processing_ms"),
        "server_observed_ms": answer.get("server_observed_ms"),
        "server_queue_wait_ms": answer.get("server_queue_wait_ms"),
        "admission_ms": answer.get("admission_ms"),
        "executor_wait_ms": answer.get("executor_wait_ms"),
        "runtime_timings": answer.get("runtime_timings"),
        "steps": answer.get("steps"),
        "provider_call_count": answer.get("provider_call_count"),
        "provider_retry_count": answer.get("provider_retry_count"),
        "input_tokens": _token_total(answer, "input_tokens"),
        "output_tokens": _token_total(answer, "output_tokens"),
        "cached_input_tokens": _token_total(answer, "cached_input_tokens"),
        "reasoning_tokens": _token_total(answer, "reasoning_tokens"),
        "query_embedding": answer.get("query_embedding"),
        "retrieved_evidence_ids": answer.get("retrieved_evidence_ids"),
        "cited_evidence_ids": _citation_ids(answer.get("citations")),
        "used_cited_evidence_ids": _passage_ids(
            [
                citation
                for citation in (answer.get("citations") or [])
                if isinstance(citation, dict) and citation.get("used") is True
            ]
        ),
        "citation_count": len(answer.get("citations") or []),
        "used_citation_count": sum(
            isinstance(citation, dict) and citation.get("used") is True
            for citation in (answer.get("citations") or [])
        ),
        "output_words": len(str(answer.get("answer") or "").split()),
        "termination_reason": answer.get("termination_reason"),
        "degraded": answer.get("degraded"),
        "retrieval_arm_failures": answer.get("retrieval_arm_failures"),
        "quality_proxy": _quality_proxy(case, answer),
    }


def _token_total(answer: dict[str, Any], field: str) -> int | None:
    steps = answer.get("steps")
    if not isinstance(steps, list):
        return None
    provider_steps = [
        step for step in steps if isinstance(step, dict) and (step.get("provider_calls") or 0)
    ]
    if not provider_steps or any(not isinstance(step.get(field), int) for step in provider_steps):
        return None
    return sum(step[field] for step in provider_steps)


def _citation_ids(citations: Any) -> list[str]:
    if not isinstance(citations, list):
        return []
    ids: list[str] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        for key in ("chunk_id", "node_id"):
            value = citation.get(key)
            if value and str(value) not in ids:
                ids.append(str(value))
    return ids


def _passage_ids(citations: Any) -> list[str]:
    if not isinstance(citations, list):
        return []
    return list(
        dict.fromkeys(
            str(citation.get("chunk_id") or citation.get("node_id"))
            for citation in citations
            if isinstance(citation, dict) and (citation.get("chunk_id") or citation.get("node_id"))
        )
    )


def _quality_proxy(case: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    text = str(answer.get("answer") or "").casefold()
    expected = case.get("expected_facts") or []
    matched = []
    for item in expected:
        variants = item if isinstance(item, list) else [item]
        if any(str(value).casefold() in text for value in variants if value):
            matched.append(item)
    allowed = {str(value) for value in case.get("acceptable_passage_ids") or []}
    cited = set(
        _passage_ids(
            [
                citation
                for citation in (answer.get("citations") or [])
                if isinstance(citation, dict) and citation.get("used") is True
            ]
        )
    )
    answerable = bool(case.get("answerable", True))
    if answerable:
        fact_coverage = len(matched) / len(expected) if expected else None
        citation_support = len(cited & allowed) / len(cited) if cited else None
        evidence_recall = len(cited & allowed) / len(allowed) if allowed else None
        return {
            "expected_fact_coverage": fact_coverage,
            "acceptable_citation_support": citation_support,
            "acceptable_evidence_recall": evidence_recall,
            "automated_pass": (
                fact_coverage == 1.0 and citation_support == 1.0
                if fact_coverage is not None and citation_support is not None
                else None
            ),
            "semantic_support_reviewed": bool(case.get("semantic_support_reviewed", False)),
        }
    refusal_terms = (
        "not found",
        "not available",
        "no evidence",
        "no matching",
        "cannot access",
        "can't access",
        "could not access",
        "not provided",
        "unable to verify",
    )
    return {
        "expected_fact_coverage": None,
        "acceptable_citation_coverage": None,
        "negative_refusal_detected": any(term in text for term in refusal_terms),
        "automated_pass": any(term in text for term in refusal_terms),
        "semantic_support_reviewed": bool(case.get("semantic_support_reviewed", False)),
    }


def _load_cases(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = [json.loads(line) for line in text.splitlines() if line.strip()]
    cases = parsed.get("cases", []) if isinstance(parsed, dict) else parsed
    if not isinstance(cases, list) or not cases:
        raise ValueError('case file must contain a non-empty JSON array or {"cases": [...]} object')
    ids: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not str(case.get("question") or "").strip():
            raise ValueError("every case must be an object with a non-empty question")
        case_id = str(case.get("id") or case.get("case_id") or "")
        if not case_id or case_id in ids:
            raise ValueError("every case needs a unique id")
        ids.add(case_id)
        if case.get("complexity") not in {"moderate", "complex", "difficult", "negative"}:
            raise ValueError(f"case {case_id!r} needs a labeled complexity")
        if not (case.get("answer_length") or case.get("depth")):
            raise ValueError(f"case {case_id!r} needs answer_length or depth")
        answerable_value = case.get("answerable", True)
        if not isinstance(answerable_value, bool):
            raise ValueError(f"case {case_id!r} must use a boolean answerable label")
        answerable = answerable_value
        if answerable and (
            not isinstance(case.get("expected_facts"), list)
            or not case["expected_facts"]
            or not isinstance(case.get("acceptable_passage_ids"), list)
            or not case["acceptable_passage_ids"]
        ):
            raise ValueError(
                f"answerable case {case_id!r} needs expected_facts and acceptable_passage_ids"
            )
        if not answerable and not str(case.get("negative_reason") or "").strip():
            raise ValueError(f"negative case {case_id!r} needs a negative_reason")
    return cases


def _probe(base_url: str, token: str, timeout: float, path: str) -> dict[str, Any] | None:
    request = urllib.request.Request(base_url.rstrip("/") + path, headers=_headers(token))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = _parse_json_or_sse(response.read())
            return body
    except Exception:
        return None


def _server_snapshot(base_url: str, token: str, timeout: float) -> dict[str, Any]:
    overview = _probe(base_url, token, timeout, "/overview") or {}
    sources = _probe(base_url, token, timeout, "/sources") or {}
    config = _probe(base_url, token, timeout, "/config") or {}
    ready = _probe(base_url, token, timeout, "/ready") or {}
    if not ready:
        ready = _probe(base_url, token, timeout, "/health") or {}
    raw_sources = (
        sources
        if isinstance(sources, list)
        else sources.get("sources")
        if isinstance(sources, dict)
        else None
    )
    if not isinstance(raw_sources, list) and isinstance(overview, dict):
        raw_sources = overview.get("sources")
    normalized = []
    for source in raw_sources or []:
        if not isinstance(source, dict):
            continue
        normalized.append(
            {
                key: source.get(key)
                for key in ("name", "type", "artifact_count", "artifacts", "last_indexed")
                if source.get(key) is not None
            }
        )
    graph_generation = ready.get("graph_generation") if isinstance(ready, dict) else None
    if isinstance(graph_generation, dict):
        graph_generation = {key: graph_generation.get(key) for key in ("loaded", "published")}
    overview_counts = {
        key: overview.get(key)
        for key in ("indexed_artifacts", "chunk_count", "node_counts", "total_nodes", "total_links")
        if overview.get(key) is not None
    }
    stable = {
        "knowledge_base": overview.get("knowledge_base") if isinstance(overview, dict) else None,
        "sources": sorted(normalized, key=lambda item: str(item.get("name", ""))),
        "overview_counts": overview_counts,
        "graph_generation": graph_generation,
    }
    effective_config = _safe_config_snapshot(config.get("effective"))
    config_hash = hashlib.sha256(
        json.dumps(effective_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    fingerprint = hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    active = any(source.get("syncing") for source in raw_sources or [] if isinstance(source, dict))
    return {
        **stable,
        "fingerprint": fingerprint,
        "indexing_active": active,
        "effective_non_secret_config": effective_config,
        "effective_config_sha256": config_hash,
    }


def _safe_config_snapshot(effective: Any) -> dict[str, Any] | None:
    """Keep only known non-secret settings; never copy raw YAML or credentials."""
    if not isinstance(effective, dict):
        return None
    assistant = effective.get("assistant") if isinstance(effective.get("assistant"), dict) else {}
    search = effective.get("search") if isinstance(effective.get("search"), dict) else {}
    embeddings = search.get("embeddings") if isinstance(search.get("embeddings"), dict) else {}
    vector_store = (
        search.get("vector_store") if isinstance(search.get("vector_store"), dict) else {}
    )
    workflow_options = assistant.get("workflow_options")
    agent_options = {}
    if isinstance(workflow_options, dict):
        candidate = workflow_options.get("agentic")
        if isinstance(candidate, dict):
            allowed_options = {
                "max_rounds",
                "per_query_results",
                "max_context_passages",
                "retrieval_modes",
                "expand_graph",
                "combine_grade_and_answer",
                "answer_max_words",
                "output_tokens_by_depth",
            }
            agent_options = {key: candidate[key] for key in allowed_options if key in candidate}
    assistant_keys = (
        "enabled",
        "provider",
        "model",
        "workflow",
        "reasoning_effort",
        "max_context_chunks",
        "max_output_tokens",
        "request_timeout_seconds",
        "latency",
        "retrieval",
    )
    embedding_keys = (
        "enabled",
        "provider",
        "model",
        "dimensions",
        "batch_size",
        "timeout_seconds",
        "query_timeout_seconds",
        "query_max_retries",
        "query_rate_limit_max_wait_seconds",
    )
    return {
        "assistant": {key: assistant[key] for key in assistant_keys if key in assistant},
        "assistant_agentic_options": agent_options,
        "search_embeddings": {key: embeddings[key] for key in embedding_keys if key in embeddings},
        "vector_store_provider": vector_store.get("provider"),
    }


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=3
        ).stdout.strip()
    except Exception:
        return None


def _container_snapshot(reference: str | None) -> dict[str, Any]:
    """Capture only image identity and resource limits from an optional Docker inspect."""
    limits: Any = os.environ.get("PHEASANT_CONTAINER_LIMITS_JSON")
    if limits:
        try:
            limits = json.loads(limits)
        except json.JSONDecodeError:
            limits = None
    snapshot: dict[str, Any] = {
        "container_id": None,
        "image_id": os.environ.get("PHEASANT_IMAGE_ID"),
        "image_ref": None,
        "started_at": None,
        "limits": limits,
        "inspect_available": False,
    }
    if not reference:
        return snapshot
    try:
        completed = subprocess.run(
            ["docker", "inspect", reference],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        inspected = json.loads(completed.stdout)[0]
    except Exception:
        return snapshot
    host = inspected.get("HostConfig") or {}
    snapshot.update(
        {
            "container_id": inspected.get("Id"),
            "image_id": inspected.get("Image") or snapshot["image_id"],
            "image_ref": (inspected.get("Config") or {}).get("Image"),
            "started_at": (inspected.get("State") or {}).get("StartedAt"),
            "limits": {
                key: host.get(key)
                for key in (
                    "NanoCpus",
                    "CpuQuota",
                    "CpuPeriod",
                    "CpusetCpus",
                    "Memory",
                    "MemorySwap",
                )
                if host.get(key) not in (None, 0, "")
            },
            "inspect_available": True,
        }
    )
    return snapshot


def _percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * probability + 0.999999)))
    return ordered[index]


def _clustered_ci(
    rows: list[dict[str, Any]], key: str, *, seed: int = 20261002
) -> list[float] | None:
    by_case: dict[str, list[float]] = {}
    for row in rows:
        value = row.get(key)
        case_id = row.get("case_id")
        if isinstance(value, (int, float)) and case_id:
            by_case.setdefault(str(case_id), []).append(float(value))
    if len(by_case) < 2:
        return None
    rng = random.Random(seed)
    ids = list(by_case)
    samples = []
    for _ in range(500):
        chosen = [rng.choice(ids) for _ in ids]
        values = [value for case_id in chosen for value in by_case[case_id]]
        estimate = _percentile(values, 0.95)
        if estimate is not None:
            samples.append(estimate)
    return [_percentile(samples, 0.025), _percentile(samples, 0.975)] if samples else None


def _summarize(rows: list[dict[str, Any]], wall_seconds: float) -> dict[str, Any]:
    completed = [
        row for row in rows if isinstance(row.get("client_completed_answer_ms"), (int, float))
    ]
    all_times = [
        float(row["client_request_end_ms"])
        for row in rows
        if row.get("client_request_end_ms") is not None
    ]
    completed_times = [float(row["client_completed_answer_ms"]) for row in completed]
    eligible = [
        row
        for row in completed
        if row.get("complexity") in {"moderate", "complex", "difficult"}
        and row.get("citation_count", 0) > 0
        and row.get("degraded") is False
        and row.get("answer_mode") != "extractive"
        and row.get("termination_reason") == "completed"
    ]
    answerable_rows = [row for row in rows if row.get("case_labels", {}).get("answerable") is True]
    answerable_completed = [
        row
        for row in answerable_rows
        if isinstance(row.get("client_completed_answer_ms"), (int, float))
    ]
    token_fields = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")
    complete_token_reports = {
        field: (
            sum(int(row[field]) for row in rows)
            if rows and all(isinstance(row.get(field), int) for row in rows)
            else None
        )
        for field in token_fields
    }
    latency_times = [float(row["client_completed_answer_ms"]) for row in eligible]
    first_answer_times = [
        float(row["first_answer_text_ms"])
        for row in rows
        if isinstance(row.get("first_answer_text_ms"), (int, float))
    ]
    sse_requests = sum(row.get("transport") == "sse" for row in rows)
    quality_rows = [row.get("quality_proxy", {}).get("automated_pass") for row in rows]
    known_quality = [value for value in quality_rows if isinstance(value, bool)]
    by_complexity = {}
    for complexity in sorted({str(row.get("complexity")) for row in rows}):
        subset = [row for row in rows if str(row.get("complexity")) == complexity]
        times = [
            float(row["client_completed_answer_ms"])
            for row in subset
            if isinstance(row.get("client_completed_answer_ms"), (int, float))
        ]
        preview_times = [
            float(row["first_answer_text_ms"])
            for row in subset
            if isinstance(row.get("first_answer_text_ms"), (int, float))
        ]
        by_complexity[complexity] = {
            "requests": len(subset),
            "distinct_questions": len({row.get("case_id") for row in subset}),
            "completed_answers": len(times),
            "completed_answer_p50_ms": _percentile(times, 0.50),
            "completed_answer_p95_ms": _percentile(times, 0.95),
            "completed_answer_p99_ms": _percentile(times, 0.99),
            "first_answer_text_requests": len(preview_times),
            "first_answer_text_p50_ms": _percentile(preview_times, 0.50),
            "first_answer_text_p95_ms": _percentile(preview_times, 0.95),
            "answerable_requests": sum(
                row.get("case_labels", {}).get("answerable") is True for row in subset
            ),
            "answerable_completed_answers": sum(
                isinstance(row.get("client_completed_answer_ms"), (int, float))
                and row.get("case_labels", {}).get("answerable") is True
                for row in subset
            ),
        }
        eligible_subset = [row for row in eligible if str(row.get("complexity")) == complexity]
        eligible_times = [float(row["client_completed_answer_ms"]) for row in eligible_subset]
        by_complexity[complexity]["eligible_quality_ungraded_answers"] = len(eligible_subset)
        by_complexity[complexity]["eligible_quality_ungraded_p95_ms"] = _percentile(
            eligible_times, 0.95
        )
    by_embedding_cache: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        metrics = row.get("query_embedding")
        if not isinstance(metrics, dict):
            category = "unavailable"
        else:
            hits = int(metrics.get("cache_hits") or 0)
            misses = int(metrics.get("fresh_misses") or 0)
            category = (
                "mixed"
                if hits and misses
                else "fresh_miss"
                if misses
                else "cache_hit"
                if hits
                else "no_vector_request"
            )
        by_embedding_cache.setdefault(category, []).append(row)
    embedding_cache_summary = {}
    for category, subset in by_embedding_cache.items():
        times = [
            float(row["client_completed_answer_ms"])
            for row in subset
            if isinstance(row.get("client_completed_answer_ms"), (int, float))
        ]
        metric_rows = [row.get("query_embedding") or {} for row in subset]
        embedding_cache_summary[category] = {
            "requests": len(subset),
            "distinct_questions": len({row.get("case_id") for row in subset}),
            "completed_answers": len(times),
            "completed_answer_p95_ms": _percentile(times, 0.95),
            "provider_requests": sum(
                int(item.get("provider_requests") or 0) for item in metric_rows
            ),
            "fresh_misses": sum(int(item.get("fresh_misses") or 0) for item in metric_rows),
            "cache_hits": sum(int(item.get("cache_hits") or 0) for item in metric_rows),
            "singleflight_waits": sum(
                int(item.get("singleflight_waits") or 0) for item in metric_rows
            ),
        }
    return {
        "requests": len(rows),
        "distinct_questions": len({row.get("case_id") for row in rows}),
        "completed_answers": len(completed),
        "completion_rate": len(completed) / len(rows) if rows else None,
        "answerable_requests": len(answerable_rows),
        "answerable_completed_answers": len(answerable_completed),
        "answerable_completion_rate": (
            len(answerable_completed) / len(answerable_rows) if answerable_rows else None
        ),
        "quality_proxy_pass_rate": (
            sum(known_quality) / len(known_quality) if known_quality else None
        ),
        "semantic_quality_adjudicated": all(
            row.get("quality_proxy", {}).get("semantic_support_reviewed") is True for row in rows
        ),
        "completed_answer_p50_ms": _percentile(completed_times, 0.50),
        "completed_answer_p95_ms": _percentile(completed_times, 0.95),
        "completed_answer_p99_ms": _percentile(completed_times, 0.99),
        "first_answer_text_rate": (
            len(first_answer_times) / sse_requests if sse_requests else None
        ),
        "first_answer_text_p50_ms": _percentile(first_answer_times, 0.50),
        "first_answer_text_p95_ms": _percentile(first_answer_times, 0.95),
        "first_answer_text_p95_cluster_ci_ms": _clustered_ci(rows, "first_answer_text_ms"),
        "eligible_quality_ungraded_p95_ms": _percentile(latency_times, 0.95),
        "eligible_quality_ungraded_p95_cluster_ci_ms": _clustered_ci(
            eligible, "client_completed_answer_ms"
        ),
        "all_outcome_p95_ms": _percentile(all_times, 0.95),
        "throughput_answers_per_second": len(completed) / wall_seconds
        if wall_seconds > 0
        else None,
        "admission_rejections": sum(bool(row.get("admission_rejected")) for row in rows),
        "timeouts": sum(bool(row.get("timeout")) for row in rows),
        "failures": sum(bool(row.get("error")) for row in rows),
        "provider_retries": sum(
            value for row in rows if isinstance((value := row.get("provider_retry_count")), int)
        ),
        "token_totals": {
            "input": complete_token_reports["input_tokens"],
            "output": complete_token_reports["output_tokens"],
            "cached_input": complete_token_reports["cached_input_tokens"],
            "reasoning": complete_token_reports["reasoning_tokens"],
            "complete_request_reports": sum(
                all(isinstance(row.get(field), int) for field in token_fields) for row in rows
            ),
            "usd_cost": None,
        },
        "complexity": by_complexity,
        "query_embedding_cache": embedding_cache_summary,
        "quality_gate": "manual evidence-support adjudication required",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--token-env", required=True, help="environment variable containing the API token"
    )
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--concurrency", required=True, type=int)
    parser.add_argument("--repeats", required=True, type=int)
    parser.add_argument("--transport", choices=("http", "sse", "mcp"), required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=float, default=240.0)
    parser.add_argument(
        "--container",
        help="optional Docker container name/ID for read-only image and limit capture",
    )
    args = parser.parse_args()
    if args.concurrency < 1 or args.repeats < 1 or args.timeout_seconds <= 0:
        parser.error("concurrency, repeats, and timeout must be positive")
    token = os.environ.get(args.token_env)
    if not token:
        parser.error(f"environment variable {args.token_env!r} is empty or unset")

    cases = _load_cases(args.cases)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    before = _server_snapshot(args.base_url, token, min(args.timeout_seconds, 10.0))
    jobs = [(case, repeat) for repeat in range(args.repeats) for case in cases]
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(
                _request_once,
                base_url=args.base_url,
                token=token,
                timeout=args.timeout_seconds,
                transport=args.transport,
                case=case,
            )
            for case, _repeat in jobs
        ]
        for (case, repeat), future in zip(jobs, futures, strict=True):
            try:
                row = future.result()
            except Exception as exc:
                row = {
                    "case_id": case.get("id"),
                    "complexity": case.get("complexity", "unknown"),
                    "transport": args.transport,
                    "error": f"benchmark worker failed: {type(exc).__name__}: {exc}",
                    "timeout": False,
                    "admission_rejected": False,
                    "client_completed_answer_ms": None,
                    "client_request_end_ms": None,
                }
            row["repeat"] = repeat + 1
            rows.append(row)
    wall_seconds = time.perf_counter() - started
    after = _server_snapshot(args.base_url, token, min(args.timeout_seconds, 10.0))
    corpus_unchanged = before["fingerprint"] == after["fingerprint"]
    configuration_unchanged = before["effective_config_sha256"] == after["effective_config_sha256"]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    result_path = args.output_dir / f"assistant-latency-{stamp}.jsonl"
    result_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    summary = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "git_commit": _git_sha(),
        "transport": args.transport,
        "base_url": args.base_url,
        "case_manifest": str(args.cases),
        "case_count": len(cases),
        "distinct_question_count": len({case.get("id") for case in cases}),
        "repeats": args.repeats,
        "concurrency": args.concurrency,
        "wall_seconds": wall_seconds,
        "corpus_before": before,
        "corpus_after": after,
        "corpus_unchanged": corpus_unchanged,
        "configuration_unchanged": configuration_unchanged,
        "comparison_valid": corpus_unchanged
        and configuration_unchanged
        and not before["indexing_active"]
        and not after["indexing_active"],
        "container": _container_snapshot(args.container),
        "summary": _summarize(rows, wall_seconds),
        "raw_results": result_path.name,
        "credentials_written": False,
    }
    summary_path = args.output_dir / f"assistant-latency-{stamp}-summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"summary": str(summary_path), "results": str(result_path), **summary["summary"]},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
