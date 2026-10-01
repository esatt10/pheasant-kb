"""Remote graph-query boundary for fleet API replicas.

The persisted graph is still the authoritative, atomically replaced snapshot.
This module changes who keeps that snapshot resident: a ``graph`` process owns
it, while API/MCP replicas hold only this small proxy.  The default URL is
``None``, so standalone installations retain the in-process behavior.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from contextlib import nullcontext
from ipaddress import ip_address
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

from pheasant.telemetry.interactions import inject_traceparent


class GraphQueryError(RuntimeError):
    """The configured graph-query service could not answer a request."""


class GraphQueryClient:
    """Small authenticated JSON client with bounded transport retries."""

    def __init__(
        self,
        base_url: str,
        token_env: str,
        timeout_seconds: float = 30.0,
        retries: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_env = token_env
        self.timeout = max(0.1, float(timeout_seconds))
        self.retries = max(0, int(retries))
        self._replica_lock = threading.Lock()
        self._replica_ordinal = 0
        self._replica_addresses: list[str] = []
        self._replica_resolved_at = 0.0
        # This endpoint is explicitly internal. Bypassing ambient HTTP_PROXY
        # also lets us address a resolved replica IP while retaining the
        # service name in Host without sending cluster traffic outside.
        self._opener = build_opener(ProxyHandler({}))

    def _target(self) -> tuple[str, str | None]:
        """Resolve every current HTTP service replica and choose round-robin."""

        parsed = urlsplit(self.base_url)
        host = parsed.hostname
        if parsed.scheme != "http" or not host:
            return f"{self.base_url}/internal/graph/query", None
        try:
            ip_address(host)
            return f"{self.base_url}/internal/graph/query", None
        except ValueError:
            pass
        port = parsed.port or 80
        with self._replica_lock:
            now = time.monotonic()
            if now - self._replica_resolved_at >= 2.0 or not self._replica_addresses:
                try:
                    addresses = []
                    for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
                        address = str(row[4][0])
                        if address not in addresses:
                            addresses.append(address)
                except OSError:
                    addresses = []
                self._replica_addresses = addresses
                self._replica_resolved_at = now
            if not self._replica_addresses:
                return f"{self.base_url}/internal/graph/query", None
            address = self._replica_addresses[self._replica_ordinal % len(self._replica_addresses)]
            self._replica_ordinal += 1
        netloc = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        path = parsed.path.rstrip("/") + "/internal/graph/query"
        target = urlunsplit((parsed.scheme, netloc, path, "", ""))
        original_host = host if parsed.port is None else f"{host}:{parsed.port}"
        return target, original_host

    def query(
        self,
        operation: str,
        *,
        timeout_seconds: float | None = None,
        retries: int | None = None,
        **parameters: Any,
    ) -> Any:
        token = os.environ.get(self.token_env or "", "")
        if not token:
            raise GraphQueryError(
                f"Graph queries require a token in environment variable {self.token_env!r}"
            )
        body = json.dumps(
            {"operation": operation, "parameters": parameters},
            separators=(",", ":"),
        ).encode("utf-8")
        last: Exception | None = None
        timeout = self.timeout if timeout_seconds is None else max(0.1, float(timeout_seconds))
        retry_count = self.retries if retries is None else max(0, int(retries))
        for attempt in range(retry_count + 1):
            target, host_header = self._target()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if host_header:
                headers["Host"] = host_header
            # Carry the caller's trace across the hop. Without it a trace stops
            # dead at this boundary: an operator sees "search took four
            # seconds" and cannot see that most of it was this one call. A
            # no-op outside a traced request, which is every sync.
            inject_traceparent(headers)
            request = Request(target, data=body, method="POST", headers=headers)
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict) or "result" not in payload:
                    raise GraphQueryError("graph service returned an invalid response")
                return payload["result"]
            except HTTPError as exc:
                try:
                    detail = json.loads(exc.read().decode("utf-8")).get("detail")
                except Exception:  # pragma: no cover - malformed upstream response
                    detail = None
                # Authentication and caller errors are deterministic; retrying
                # would only delay the useful error.
                if 400 <= exc.code < 500:
                    raise GraphQueryError(
                        f"graph service rejected {operation!r}: {detail or exc.reason}"
                    ) from exc
                last = exc
            except (OSError, TimeoutError, URLError, json.JSONDecodeError) as exc:
                last = exc
            if attempt < retry_count:
                time.sleep(0.05 * (attempt + 1))
        raise GraphQueryError(
            f"graph service at {self.base_url!r} could not answer {operation!r}: {last}"
        ) from last


class RemoteNodeView:
    """The bounded subset of NetworkX's node view used by serving code."""

    def __init__(self, graph: RemoteGraph) -> None:
        self.graph = graph

    def get(self, key: str, default: Any = None) -> Any:
        value = self.graph.get_node(key)
        return default if value is None else value

    def __getitem__(self, key: str) -> dict[str, Any]:
        value = self.graph.get_node(key)
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key: str) -> bool:
        return self.graph.get_node(key) is not None


class RemoteGraph:
    """Query-shaped proxy that never materializes the full graph locally."""

    is_remote_graph = True

    def __init__(self, client: GraphQueryClient, *, stats_ttl_seconds: float = 2.0) -> None:
        self.client = client
        self.nodes = RemoteNodeView(self)
        self._stats_ttl = max(0.0, float(stats_ttl_seconds))
        self._stats_value: dict[str, Any] | None = None
        self._stats_at = 0.0
        self._stats_lock = threading.Lock()

    def reading(self):
        # A remote operation is internally pinned by the service. Cross-call
        # transactions are intentionally not promised by this proxy.
        return nullcontext()

    def stats(self, *, fresh: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        with self._stats_lock:
            if (
                not fresh
                and self._stats_value is not None
                and now - self._stats_at <= self._stats_ttl
            ):
                return dict(self._stats_value)
            value = dict(self.client.query("stats"))
            self._stats_value = value
            self._stats_at = now
            return dict(value)

    def ping(self) -> dict[str, Any]:
        return self.stats(fresh=True)

    def number_of_nodes(self) -> int:
        return int(self.stats().get("total_nodes") or 0)

    def number_of_edges(self) -> int:
        return int(self.stats().get("total_links") or 0)

    def type_counts(self) -> dict[str, int]:
        return {str(k): int(v) for k, v in (self.stats().get("node_types") or {}).items()}

    def __len__(self) -> int:
        return self.number_of_nodes()

    def __contains__(self, node_id: str) -> bool:
        return self.get_node(node_id) is not None

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        value = self.client.query("node", node_id=node_id)
        return dict(value) if isinstance(value, dict) else None

    def remote_search(self, **parameters: Any) -> list[dict[str, Any]]:
        return list(self.client.query("search", **parameters) or [])

    def remote_neighbors(self, **parameters: Any) -> dict[str, Any]:
        return dict(self.client.query("neighbors", **parameters))

    def remote_slice(self, **parameters: Any) -> dict[str, Any]:
        return dict(self.client.query("slice", **parameters))

    def remote_node_link(self, **parameters: Any) -> dict[str, Any]:
        return dict(self.client.query("node_link", **parameters))

    def remote_cytoscape(self) -> dict[str, Any]:
        return dict(self.client.query("cytoscape"))

    def remote_diagnostics(self, top: int = 20) -> dict[str, Any]:
        return dict(self.client.query("diagnostics", top=top))

    def remote_path(self, source: str, target: str, max_depth: int = 8) -> dict[str, Any]:
        return dict(self.client.query("path", source=source, target=target, max_depth=max_depth))

    def remote_taxonomy(
        self,
        source: str | None = None,
        path: str | None = None,
        max_nodes: int = 2000,
    ) -> dict[str, Any]:
        return dict(self.client.query("taxonomy", source=source, path=path, max_nodes=max_nodes))

    def remote_facts(self, node_ids: list[str], limit: int = 12) -> list[dict[str, Any]]:
        # Facts decorate an answer but do not ground it. Keep a congested graph
        # service from holding synthesis for its full query deadline or retry.
        return list(
            self.client.query(
                "facts",
                timeout_seconds=min(self.client.timeout, 3.0),
                retries=0,
                node_ids=node_ids,
                limit=limit,
            )
            or []
        )

    def remote_figures(self, node_ids: list[str], limit: int = 8) -> list[dict[str, Any]]:
        # Figures decorate an answer; keep a congested graph from holding the
        # answer for the full query deadline or retry window.
        return list(
            self.client.query(
                "figures",
                timeout_seconds=min(self.client.timeout, 3.0),
                retries=0,
                node_ids=node_ids,
                limit=limit,
            )
            or []
        )

    def remote_memory_coverage(self, artifact_ids: list[str]) -> dict[str, Any]:
        return dict(self.client.query("memory_coverage", artifact_ids=artifact_ids))

    def to_node_link(self) -> dict[str, Any]:
        return self.remote_node_link()


class LiveLocalGraph:
    """Delegate to the engine's current generation after atomic reloads."""

    def __init__(self, getter: Any) -> None:
        self._getter = getter

    @property
    def nodes(self) -> Any:
        return self._getter().nodes

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._getter()

    def __len__(self) -> int:
        return len(self._getter())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._getter(), name)


def graph_for_config(config: Any, local_graph: Any, *, force_local: bool = False) -> Any:
    """Return the serving graph selected by config without changing defaults."""

    settings = getattr(config, "graph", None)
    url = str(getattr(settings, "query_service_url", "") or "").strip()
    if force_local or not url:
        return LiveLocalGraph(local_graph) if callable(local_graph) else local_graph
    return RemoteGraph(
        GraphQueryClient(
            url,
            str(getattr(settings, "query_service_token_env", "") or ""),
            float(getattr(settings, "query_service_timeout_seconds", 30.0) or 30.0),
        )
    )
