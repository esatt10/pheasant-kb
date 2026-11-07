"""Remote graph-query boundary for fleet API replicas.

The persisted graph is still the authoritative, atomically replaced snapshot.
This module changes who keeps that snapshot resident: a ``graph`` process owns
it, while API/MCP replicas hold only this small proxy.  The default URL is
``None``, so standalone installations retain the in-process behavior.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from contextlib import nullcontext
from functools import wraps
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pheasant.security.service_auth import outbound_token
from pheasant.telemetry.interactions import inject_traceparent


def _timed_graph_query(fn):
    @wraps(fn)
    def wrapped(self, operation: str, *args, **kwargs):
        started = time.perf_counter()
        try:
            return fn(self, operation, *args, **kwargs)
        finally:
            from pheasant.request_budget import record_active_timing

            record_active_timing(
                "graph_http",
                time.perf_counter() - started,
                operation=operation,
            )

    return wrapped


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
        identity: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_env = token_env
        #: `security.service_identity`: a managed identity stands in for an
        #: unset token (`security/service_auth.py`).
        self.identity = identity
        self.timeout = max(0.1, float(timeout_seconds))
        self.retries = max(0, int(retries))
        self._replica_lock = threading.Lock()
        self._replica_ordinal = 0
        self._replica_addresses: list[str] = []
        self._replica_resolved_at = 0.0
        # Kept-alive connections, one set per thread (http.client connections
        # are not thread-safe), keyed by the resolved replica. Not urllib:
        # it sends `Connection: close` on every request and cannot be told
        # otherwise, and the server then closes the socket straight after its
        # answer. Over a real network hop (Azure Container Apps) that close
        # cut every answer larger than the send buffer short, 65 KB of a
        # 363 KB node_link, while a kept-alive connection read all of it.
        # http.client also ignores ambient HTTP_PROXY, as this internal hop
        # must, and lets us address a resolved replica IP with the service
        # name in Host.
        self._local = threading.local()

    def _post(
        self, target: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, str, bytes]:
        """POST over a kept-alive connection to ``target``'s replica.

        A pooled connection the server has since closed (uvicorn drops an
        idle one after a few seconds) fails before any answer arrives; that
        is reopened once here rather than spent as one of the caller's
        retries, because nothing was lost but a stale socket.
        """

        parsed = urlsplit(target)
        key = (parsed.scheme, parsed.hostname, parsed.port)
        connections: dict[tuple[Any, ...], http.client.HTTPConnection]
        connections = self._local.__dict__.setdefault("connections", {})
        secure = parsed.scheme == "https"
        factory = http.client.HTTPSConnection if secure else http.client.HTTPConnection
        path = parsed.path or "/"
        for attempt in range(2):
            connection = connections.get(key)
            reused = connection is not None
            if connection is None:
                connection = factory(parsed.hostname or "", parsed.port, timeout=timeout)
                connections[key] = connection
            connection.timeout = timeout
            if connection.sock is not None:
                connection.sock.settimeout(timeout)
            try:
                connection.request("POST", path, body=body, headers=headers)
                response = connection.getresponse()
                data = response.read()
            # RemoteDisconnected, a broken pipe or a reset: how a dead socket
            # shows depends on the platform, and all are ConnectionErrors.
            except ConnectionError:
                connection.close()
                connections.pop(key, None)
                if reused and attempt == 0:
                    continue
                raise
            except BaseException:
                connection.close()
                connections.pop(key, None)
                raise
            if response.will_close:
                connection.close()
                connections.pop(key, None)
            return response.status, response.reason, data
        raise AssertionError("unreachable")  # pragma: no cover

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

    @_timed_graph_query
    def query(
        self,
        operation: str,
        *,
        timeout_seconds: float | None = None,
        retries: int | None = None,
        **parameters: Any,
    ) -> Any:
        token = outbound_token(self.token_env, self.identity)
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
        from pheasant.request_budget import DeadlineExceeded, remaining_seconds

        remaining = remaining_seconds()
        if remaining is not None:
            if remaining <= 0:
                raise DeadlineExceeded("assistant request deadline exceeded")
            timeout = min(timeout, remaining)
        retry_count = self.retries if retries is None else max(0, int(retries))
        for attempt in range(retry_count + 1):
            remaining = remaining_seconds()
            if remaining is not None:
                if remaining <= 0:
                    raise DeadlineExceeded("assistant request deadline exceeded")
                timeout = min(timeout, remaining)
            target, host_header = self._target()
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if remaining is not None:
                headers["X-Pheasant-Remaining-Ms"] = str(max(1, int(remaining * 1000)))
            if host_header:
                headers["Host"] = host_header
            # Carry the caller's trace across the hop. Without it a trace stops
            # dead at this boundary: an operator sees "search took four
            # seconds" and cannot see that most of it was this one call. A
            # no-op outside a traced request, which is every sync.
            inject_traceparent(headers)
            try:
                status, reason, data = self._post(target, body, headers, timeout)
                if status >= 400:
                    try:
                        detail = json.loads(data.decode("utf-8")).get("detail")
                    except Exception:  # pragma: no cover - malformed upstream response
                        detail = None
                    # Authentication and caller errors are deterministic;
                    # retrying would only delay the useful error.
                    if 400 <= status < 500:
                        raise GraphQueryError(
                            f"graph service rejected {operation!r}: {detail or reason}"
                        )
                    last = GraphQueryError(f"HTTP {status} {detail or reason}")
                else:
                    payload = json.loads(data.decode("utf-8"))
                    if not isinstance(payload, dict) or "result" not in payload:
                        raise GraphQueryError("graph service returned an invalid response")
                    return payload["result"]
            # `http.client.HTTPException` is not an OSError: a truncated body
            # (IncompleteRead) used to escape this loop unretried, as a raw
            # exception and a 500 from whichever surface had asked.
            except (OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError) as exc:
                remaining = remaining_seconds()
                if remaining is not None and remaining <= 0:
                    raise DeadlineExceeded("assistant request deadline exceeded") from exc
                last = exc
            if attempt < retry_count:
                delay = 0.05 * (attempt + 1)
                remaining = remaining_seconds()
                if remaining is not None:
                    if remaining <= 0 or delay >= remaining:
                        if remaining > 0:
                            time.sleep(remaining)
                        raise DeadlineExceeded("assistant request deadline exceeded")
                    delay = min(delay, remaining)
                time.sleep(delay)
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


#: Node ids one ``edges_batch`` request may name.
MAX_EDGE_BATCH = 500


def edges_batch(
    graph: Any, direction: str, node_ids: list[str], exclude_types: set[str]
) -> dict[str, list[list[Any]]]:
    """``{node_id: [[source, target, [types]], ...]}``: one batch's in- or out-edges.

    The graph service's half of :meth:`RemoteGraph.remote_edges_batch`. An
    endpoint pair and its link types, nothing else, and no pair whose every
    type is excluded: `services.inventory_detail` asked for the in-links of
    one document by reading the *out*-edges of every document in the region,
    16 at a time with every neighbour attached, and at 18,000 documents the
    api replica ran out of memory holding them.
    """

    if direction not in {"in", "out"}:
        raise ValueError("direction must be 'in' or 'out'")
    if len(node_ids) > MAX_EDGE_BATCH:
        raise ValueError(f"edges_batch takes at most {MAX_EDGE_BATCH} nodes")
    fetch = graph.in_edges_batch if direction == "in" else graph.out_edges_batch
    answer: dict[str, list[list[Any]]] = {}
    with graph.reading():
        for node_id, entries in fetch([str(n) for n in node_ids]).items():
            rows = []
            for source, target, edge_map in entries:
                values = edge_map.values() if hasattr(edge_map, "values") else edge_map
                types = {str(d.get("type")) for d in values if d and d.get("type")}
                if kept := sorted(types - exclude_types):
                    rows.append([str(source), str(target), kept])
            answer[str(node_id)] = rows
    return answer


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

    def remote_neighbors_many(self, node_ids: list[str], depth: int = 1) -> list[dict[str, Any]]:
        """Expand several answer hits through one graph-service request."""
        answers: list[dict[str, Any]] = []
        for offset in range(0, len(node_ids), 16):
            batch = node_ids[offset : offset + 16]
            try:
                result = self.client.query("neighbors_many", node_ids=batch, depth=depth)
            except GraphQueryError as exc:
                # During a rolling deployment an older graph service can still
                # answer the existing single-node operation.
                if "unknown graph operation: neighbors_many" not in str(exc):
                    raise
                result = [self.remote_neighbors(node_id=node_id, depth=depth) for node_id in batch]
            if not isinstance(result, list) or len(result) != len(batch):
                raise GraphQueryError("graph service returned an invalid neighbors_many response")
            for node_id, item in zip(batch, result, strict=True):
                if (
                    not isinstance(item, dict)
                    or item.get("node_id") != node_id
                    or not isinstance(item.get("neighbors"), list)
                ):
                    raise GraphQueryError(
                        "graph service returned an invalid neighbors_many response"
                    )
                answers.append(dict(item))
        return answers

    def remote_edges_batch(
        self, direction: str, node_ids: list[str], exclude_types: set[str] | frozenset[str]
    ) -> dict[str, list[list[Any]]] | None:
        """In- or out-edges of ``node_ids`` as endpoint pairs and types (:func:`edges_batch`).

        ``None`` from a graph service older than the operation, so a caller
        keeps its previous path through a rolling deployment.
        """

        answer: dict[str, list[list[Any]]] = {}
        for offset in range(0, len(node_ids), MAX_EDGE_BATCH):
            batch = node_ids[offset : offset + MAX_EDGE_BATCH]
            try:
                result = self.client.query(
                    "edges_batch",
                    direction=direction,
                    node_ids=batch,
                    exclude_types=sorted(exclude_types),
                )
            except GraphQueryError as exc:
                if "unknown graph operation: edges_batch" in str(exc):
                    return None
                raise
            answer.update(result or {})
        return answer

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
            identity=getattr(getattr(config, "security", None), "service_identity", None),
        )
    )
