"""gRPC transport for remote preparation (Phase 35.5).

An alternative wire format for the work :mod:`pheasant.sync.worker_pool`
already governs. It plugs into the same transport interface as
:class:`~pheasant.sync.worker_transport.HttpTransport`, so retry, jitter,
circuit breakers, failover, local fallback, deadlines and idempotency keys
are inherited rather than reimplemented — the reason those live in the pool
and not in the transport.

What gRPC actually buys here, measured against the hardened HTTP path:

* **Raw bytes.** The JSON transport base64s every file, a flat 33% inflation
  on the only large field. ``PrepareRequest.content`` is ``bytes``.
* **Streaming.** ``PrepareBatch`` is bidirectional, so results come back as
  they are computed instead of after the slowest file in the batch.
* **Native deadlines.** A gRPC deadline is enforced by the runtime on both
  sides, where the HTTP transport has to carry a header and trust the peer
  to read it.

**No generated code is checked in.** The stubs are compiled from the vendored
``.proto`` at import time via :func:`grpc.protos_and_services`. Vendoring
protobuf gencode looked simpler and is not: modern ``protoc`` output calls
``ValidateProtobufRuntimeVersion`` with the exact runtime it was generated
against, so a checked-in stub silently pins every installer to one protobuf
release — and its generated ``import pheasant_worker_pb2`` is not
package-relative, so it would not resolve inside the package anyway. Paying
``grpcio-tools`` in the optional ``[grpc]`` extra buys a ``.proto`` that is
genuinely the contract, compiled against whatever protobuf the installer has.

Only the structured fields are JSON inside the proto; the file's bytes are
``bytes``. Mirroring pheasant's config dataclasses into protobuf messages
would make every config change a proto change, for fields that are a few
hundred bytes next to a payload that can be megabytes.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from pheasant.sync.remote_worker import RemoteWorkerError

logger = logging.getLogger(__name__)

PROTO_PATH = Path(__file__).with_name("proto") / "pheasant_worker.proto"

SERVICE = "pheasant.worker.v1.PreparationWorker"
PREPARE_METHOD = f"/{SERVICE}/PrepareBatch"
CHECK_METHOD = f"/{SERVICE}/Check"

DEFAULT_PORT = 8766

#: A single message must fit the file it carries. Allow the 1 GiB configured
#: file limit plus room for protobuf metadata; the coordinator's batch size
#: bounds the rest.
MAX_MESSAGE_BYTES = 1024 * 1024 * 1024 + 16 * 1024 * 1024

_LOADED: tuple[Any, Any] | None = None
_LOAD_LOCK = threading.Lock()


class GrpcUnavailable(RemoteWorkerError):
    """``sync.concurrency.worker_transport: grpc`` without the ``[grpc]`` extra.

    Raised loudly rather than falling back to HTTP: an operator who asked for
    gRPC and silently got JSON would see the base64 inflation they chose this
    transport to avoid, and would have no way to tell.
    """


def load_protos() -> tuple[Any, Any]:
    """Compile the vendored ``.proto`` once per process.

    Compiling twice raises ``TypeError: Couldn't build proto file`` from the
    descriptor pool, so this is a cached singleton rather than a helper.
    """

    global _LOADED
    if _LOADED is not None:
        return _LOADED
    with _LOAD_LOCK:
        if _LOADED is not None:
            return _LOADED
        try:
            import grpc
        except ImportError as exc:  # pragma: no cover - exercised by the gate test
            raise GrpcUnavailable(
                "the gRPC worker transport needs the [grpc] extra: pip install 'pheasant[grpc]'"
            ) from exc
        import sys

        # protos_and_services resolves the file against sys.path, and the
        # generated service module imports its own message module by bare
        # name, so the proto's directory has to be importable for the
        # duration of the compile.
        directory = str(PROTO_PATH.parent)
        added = directory not in sys.path
        if added:
            sys.path.insert(0, directory)
        try:
            _LOADED = grpc.protos_and_services(PROTO_PATH.name)
        except ImportError as exc:
            raise GrpcUnavailable(
                "the gRPC worker transport needs grpcio-tools to compile its "
                ".proto: pip install 'pheasant[grpc]'"
            ) from exc
        finally:
            if added:
                try:
                    sys.path.remove(directory)
                except ValueError:  # pragma: no cover - concurrent mutation
                    pass
    return _LOADED


def grpc_available() -> bool:
    try:
        load_protos()
    except GrpcUnavailable:
        return False
    return True


def _target(url: str) -> str:
    """``grpc://host:port``, ``host:port`` and a bare host all address a worker."""

    target = url.strip()
    for prefix in ("grpc://", "grpcs://", "http://", "https://"):
        if target.startswith(prefix):
            target = target[len(prefix) :]
            break
    target = target.rstrip("/")
    if ":" not in target:
        target = f"{target}:{DEFAULT_PORT}"
    return target


def _secure(url: str) -> bool:
    return url.strip().startswith(("grpcs://", "https://"))


# --------------------------------------------------------------------------
# Coordinator side
# --------------------------------------------------------------------------


class GrpcTransport:
    """Speak :mod:`pheasant.sync.worker_transport`'s interface over gRPC.

    One channel per service endpoint multiplexes concurrent streams over
    HTTP/2. Sharing it across a whole source is both correct and the point:
    its round-robin picker sees every RPC and distributes batches across all
    addresses returned for a scaled Docker or cluster service.
    """

    def __init__(self) -> None:
        self._pb2, self._pb2_grpc = load_protos()
        self._channels: dict[str, Any] = {}
        self._channels_lock = threading.Lock()

    def _new_channel(self, url: str) -> Any:
        import grpc

        options = [
            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
            ("grpc.keepalive_time_ms", 30_000),
            # Docker's scaled service name resolves to every worker replica.
            # gRPC otherwise defaults to pick_first and pins this long-lived
            # HTTP/2 channel to one container for the entire source sync.
            ("grpc.lb_policy_name", "round_robin"),
        ]
        target = _target(url)
        if _secure(url):
            return grpc.secure_channel(target, grpc.ssl_channel_credentials(), options=options)
        return grpc.insecure_channel(target, options=options)

    def _channel(self, url: str) -> Any:
        """Return one thread-safe multiplexed channel per service endpoint.

        Sharing is important for DNS-backed worker fleets: one round-robin
        picker sees every concurrent batch and distributes the RPCs across all
        resolved replicas. A separate channel per caller would give every
        picker its own first choice and could still concentrate the first wave
        of a sync on one worker.
        """

        with self._channels_lock:
            channel = self._channels.get(url)
            if channel is None:
                channel = self._new_channel(url)
                self._channels[url] = channel
            return channel

    def _drop_channel(self, url: str, channel: Any) -> None:
        with self._channels_lock:
            if self._channels.get(url) is channel:
                self._channels.pop(url, None)
        self.discard(channel)

    def discard(self, connection: Any) -> None:
        try:
            connection.close()
        except Exception:  # pragma: no cover - closing a dead channel
            pass

    def post(
        self,
        connection: Any | None,
        url: str,
        tasks: list[dict[str, Any]],
        keys: list[str],
        *,
        token: str,
        timeout: float,
        deadline_seconds: float | None,
    ) -> tuple[dict[str, Any], Any | None]:
        import grpc

        channel = connection if connection is not None else self._channel(url)
        stub = self._pb2_grpc.PreparationWorkerStub(channel)
        budget = timeout if deadline_seconds is None else max(0.001, min(timeout, deadline_seconds))
        requests = [
            request_from_task(self._pb2, task, keys[position] if position < len(keys) else "")
            for position, task in enumerate(tasks)
        ]
        try:
            responses = list(
                stub.PrepareBatch(
                    iter(requests),
                    timeout=budget,
                    metadata=(("authorization", f"Bearer {token}"),),
                )
            )
        except grpc.RpcError as exc:
            self._drop_channel(url, channel)
            raise _from_rpc_error(exc, url) from exc
        results: list[dict[str, Any]] = []
        for response in responses:
            if response.error:
                results.append({"error": response.error})
                continue
            results.append(
                {"parsed": json.loads(response.parsed_json) if response.parsed_json else None}
            )
        # gRPC channels are thread-safe and multiplex streams. Keep this one
        # in the transport-wide cache instead of handing it to WorkerPool's
        # exclusive HTTP connection pool.
        return {"results": results}, None

    def close(self) -> None:
        with self._channels_lock:
            channels = list(self._channels.values())
            self._channels.clear()
        for channel in channels:
            self.discard(channel)


def _from_rpc_error(exc: Any, url: str) -> Exception:
    """Map a gRPC status onto the pool's retryable/permanent split."""

    import grpc

    from pheasant.sync.worker_pool import TaskRejected, _Retryable

    code = exc.code()
    detail = (exc.details() or "")[:200]
    retryable = {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.ABORTED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.UNKNOWN,
    }
    if code in retryable:
        return _Retryable(f"{code.name} from {url}: {detail}")
    if code in {grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED}:
        return RemoteWorkerError(f"{code.name} from {url}: {detail}")
    if code == grpc.StatusCode.UNIMPLEMENTED:
        # The peer is not a pheasant preparation worker, or is too old. That
        # is an endpoint problem, not a task problem, so let the breaker take
        # it out of rotation rather than blaming every file that lands on it.
        return RemoteWorkerError(f"{code.name} from {url}: {detail}")
    return TaskRejected(f"{code.name} from {url}: {detail}")


def request_from_task(pb2: Any, task: dict[str, Any], key: str) -> Any:
    """Task dict -> ``PrepareRequest``, moving content out of base64."""

    import base64

    payload = dict(task.get("payload") or {})
    encoded = payload.pop("content_base64", "")
    content = base64.b64decode(encoded, validate=True) if encoded else b""
    git_metadata = task.get("git_metadata")
    return pb2.PrepareRequest(
        idempotency_key=key,
        source_json=json.dumps(task.get("source") or {}, separators=(",", ":")),
        item_json=json.dumps(task.get("item") or {}, separators=(",", ":")),
        content=content,
        payload_meta_json=json.dumps(payload, separators=(",", ":")),
        git_metadata_json=(
            "" if git_metadata is None else json.dumps(git_metadata, separators=(",", ":"))
        ),
    )


def task_from_request(request: Any) -> dict[str, Any]:
    """``PrepareRequest`` -> the task dict :func:`prepare_task` accepts.

    The inverse of :func:`request_from_task`, so one preparation
    implementation serves both transports. A second parse path is exactly the
    kind of divergence that makes two wire formats a liability.
    """

    import base64

    payload = json.loads(request.payload_meta_json) if request.payload_meta_json else {}
    payload["content_base64"] = base64.b64encode(request.content).decode("ascii")
    return {
        "source": json.loads(request.source_json) if request.source_json else {},
        "item": json.loads(request.item_json) if request.item_json else {},
        "payload": payload,
        "git_metadata": (
            json.loads(request.git_metadata_json) if request.git_metadata_json else None
        ),
    }


# --------------------------------------------------------------------------
# Worker side
# --------------------------------------------------------------------------


class PreparationWorkerServicer:
    """Serve preparation over gRPC.

    Deliberately mirrors ``/internal/indexing/prepare-batch``: same gate, same
    constant-time bearer check, same bounded result cache, same rule that a
    task whose caller has given up is declined rather than computed.
    """

    def __init__(self, config: Any, cache: Any | None = None, *, version: str = "") -> None:
        from pheasant.sync.remote_worker import ResultCache

        self.config = config
        self.cache = cache if cache is not None else ResultCache()
        self.version = version

    def _authenticate(self, context: Any) -> None:
        import hmac

        import grpc

        from pheasant.sync.remote_worker import configured_token

        concurrency = self.config.sync.concurrency
        if not concurrency.remote_worker_enabled:
            context.abort(grpc.StatusCode.UNIMPLEMENTED, "remote indexing worker is disabled")
        try:
            expected = configured_token(concurrency.remote_worker_token_env)
        except RemoteWorkerError as exc:
            context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        supplied = ""
        for name, value in context.invocation_metadata():
            if name.lower() == "authorization":
                scheme, _, supplied = str(value).partition(" ")
                if scheme.lower() != "bearer":
                    supplied = ""
                break
        if not supplied or not hmac.compare_digest(supplied, expected):
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid indexing worker token")

    def PrepareBatch(self, request_iterator: Any, context: Any):  # noqa: N802 - gRPC spelling
        import grpc

        from pheasant.sync.remote_worker import prepare_task

        pb2, _ = load_protos()
        self._authenticate(context)
        prepared_count = 0
        cached_count = 0
        refused_count = 0
        for request in request_iterator:
            remaining = context.time_remaining()
            if remaining is not None and remaining <= 0:
                context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "caller deadline passed")
            key = request.idempotency_key
            if key:
                found, cached = self.cache.get(key)
                if found:
                    cached_count += 1
                    yield pb2.PrepareResponse(
                        idempotency_key=key,
                        parsed_json=json.dumps(cached) if cached is not None else "",
                        cached=True,
                    )
                    continue
            try:
                parsed = prepare_task(task_from_request(request))
            except (KeyError, TypeError, ValueError, RemoteWorkerError) as exc:
                # Per-item, not per-stream: streaming is what makes it
                # possible to refuse one file and still serve the rest, and
                # the coordinator prepares the refused one locally.
                refused_count += 1
                yield pb2.PrepareResponse(idempotency_key=key, error=str(exc))
                continue
            if key:
                self.cache.put(key, parsed)
            prepared_count += 1
            yield pb2.PrepareResponse(
                idempotency_key=key,
                parsed_json=json.dumps(parsed) if parsed is not None else "",
            )
        logger.info(
            "gRPC preparation batch complete: prepared=%d cached=%d refused=%d",
            prepared_count,
            cached_count,
            refused_count,
        )

    def Check(self, request: Any, context: Any):  # noqa: N802 - gRPC spelling
        pb2, _ = load_protos()
        concurrency = self.config.sync.concurrency
        return pb2.HealthResponse(
            ready=bool(concurrency.remote_worker_enabled), version=self.version
        )


def serve(
    config: Any,
    *,
    host: str = "0.0.0.0",  # noqa: S104 - a worker is addressed from other pods
    port: int = DEFAULT_PORT,
    max_workers: int = 8,
    version: str = "",
) -> Any:
    """Start a gRPC preparation worker and return the running server."""

    from concurrent.futures import ThreadPoolExecutor

    # `load_protos` first, deliberately. It is the function whose whole job is
    # to turn a missing extra into `GrpcUnavailable`, which is what the CLI
    # catches to print one actionable line. A bare `import grpc` above it
    # raised ModuleNotFoundError straight past that handler, so `pheasant
    # worker` on an image without the [grpc] extra greeted an operator with a
    # traceback instead — on the tier that scales hardest and comes up last.
    _pb2, pb2_grpc = load_protos()

    import grpc

    server = grpc.server(
        ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="pheasant-grpc"),
        options=[
            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        ],
    )
    pb2_grpc.add_PreparationWorkerServicer_to_server(
        PreparationWorkerServicer(config, version=version), server
    )
    bound = server.add_insecure_port(f"{host}:{port}")
    server.start()
    logger.info("gRPC preparation worker listening on %s:%s", host, bound)
    return server
