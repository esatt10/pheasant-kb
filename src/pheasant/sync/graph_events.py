"""Announcing a graph commit, so a replica need not go looking (Phase 35.8).

The graph is a file on a shared volume. An indexer writes it; every serving
process polls for it and swaps generations atomically. That is
shared-storage integration between services — the coupling style the whole
microservice literature exists to discourage — and it carries three costs:
a ReadWriteMany requirement that rules out most default StorageClasses, a
window in which replicas disagree about what the region contains, and a
failure mode (a stale graph) that is silent by construction.

The storage question is a larger design decision and is not this module. The
*window* is, and it collapses to commit latency with one message on the broker
the topology already runs:

    indexer  --publish--> pheasant.graph.committed.<kb>  --> every replica
                                                             reloads at once

Three properties this deliberately has, each of which is why it is core NATS
rather than JetStream:

**Fan-out, not work-sharing.** Every replica must reload; a work queue would
hand the message to exactly one of them, which is the opposite. JetStream is
right for `index_tasks` — the task must be done once — and wrong here.

**At-most-once, on purpose.** A dropped notification costs one poll interval,
because the poll is *kept* as a backstop rather than replaced. So this needs
no durable consumer, no acks, no per-replica state in the broker, and no
cleanup when a pod goes away. A design that had to guarantee delivery would
have to own all four.

**Fail-soft in both directions.** A publish failure is logged and swallowed —
a commit that has already reached disk must not be undone by a broker being
down — and a region with no broker at all keeps the polling refresher it has
always had, unchanged. Rule 7: the no-infrastructure path is the default and
stays the default.

The payload is small and content-addressed: the generation id from the
publication record, its counts, and the knowledge base it belongs to. A
subscriber uses it as a *hint to look*, never as data — it re-reads the
record from `/state` before deciding anything, so a forged or stale message
can cost a wasted stat and nothing else.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Subject prefix. The kb id is appended so two regions sharing a broker do
#: not wake each other — a wasted reload is cheap, but a fleet-wide one on
#: every neighbour's commit is not.
DEFAULT_SUBJECT = "pheasant.graph.committed"


def subject_for(base: str, kb_id: str) -> str:
    """One subject per knowledge base, sanitized for NATS' token grammar."""

    safe = "".join(character if character.isalnum() else "-" for character in str(kb_id))
    return f"{(base or DEFAULT_SUBJECT).rstrip('.')}.{safe or 'default'}"


class GraphCommitNotifier:
    """The seam. The null implementation *is* the standalone behavior."""

    enabled = False

    def publish(self, kb_id: str, record: dict[str, Any]) -> bool:
        """Announce a committed generation. Never raises."""

        return False

    def subscribe(self, kb_id: str, handler: Callable[[], None]) -> bool:
        """Call ``handler`` when a generation is committed. Never raises.

        ``handler`` must be cheap and non-blocking — setting an event is the
        intended shape. The reload it triggers takes seconds on a large graph
        and belongs on the refresher's own thread, not on the broker client's.
        """

        return False

    def close(self) -> None:
        return None


class NatsGraphNotifier(GraphCommitNotifier):
    """Core NATS pub/sub over the fleet's existing broker."""

    enabled = True

    def __init__(
        self,
        servers: list[str],
        *,
        subject: str = DEFAULT_SUBJECT,
        connect_timeout: float = 5.0,
    ) -> None:
        try:
            import nats  # noqa: F401
        except ImportError as exc:  # pragma: no cover - guarded by the factory
            raise RuntimeError(
                "graph commit events need the [queue] extra: pip install 'pheasant[queue]'"
            ) from exc
        self.servers = list(servers)
        self.subject = subject
        self.connect_timeout = float(connect_timeout)
        self._loop: Any = None
        self._thread: threading.Thread | None = None
        self._client: Any = None
        self._lock = threading.RLock()

    # One private event loop on a daemon thread, driven continuously — the
    # same shape and the same reason as `NatsQueue._run`: nats-py's socket
    # reader and keepalive coroutine only run while something drives the loop,
    # and a client that is idle between publishes is exactly this one.
    def _run(self, coroutine: Any) -> Any:
        import asyncio

        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=self._loop.run_forever,
                    name="pheasant-graph-events-loop",
                    daemon=True,
                )
                self._thread.start()
            loop = self._loop
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result()

    def _connect(self) -> Any:
        with self._lock:
            if self._client is not None and getattr(self._client, "is_connected", False):
                return self._client

            async def setup() -> Any:
                import nats

                return await nats.connect(
                    servers=self.servers, connect_timeout=self.connect_timeout
                )

            self._client = self._run(setup())
            return self._client

    def publish(self, kb_id: str, record: dict[str, Any]) -> bool:
        body = json.dumps({"kb_id": kb_id, **record}, sort_keys=True, default=str)

        async def send(client: Any) -> None:
            await client.publish(subject_for(self.subject, kb_id), body.encode("utf-8"))

        try:
            self._run(send(self._connect()))
        except Exception:  # noqa: BLE001 - a commit is already durable on disk
            logger.warning("Could not announce the graph generation", exc_info=True)
            return False
        return True

    def subscribe(self, kb_id: str, handler: Callable[[], None]) -> bool:
        async def on_message(_message: Any) -> None:
            # Deliberately ignores the payload: the subscriber re-reads the
            # publication record from /state, so a message is a hint to look
            # rather than a fact to trust.
            try:
                handler()
            except Exception:  # noqa: BLE001 - a bad handler must not kill the client
                logger.warning("A graph-commit handler raised", exc_info=True)

        async def listen(client: Any) -> None:
            await client.subscribe(subject_for(self.subject, kb_id), cb=on_message)

        try:
            self._run(listen(self._connect()))
        except Exception:  # noqa: BLE001 - the poll is the backstop
            logger.warning(
                "Could not subscribe to graph commit events; falling back to polling only",
                exc_info=True,
            )
            return False
        return True

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
            loop, self._loop = self._loop, None
        if client is not None and loop is not None:
            try:
                import asyncio

                asyncio.run_coroutine_threadsafe(client.drain(), loop).result(timeout=5)
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.debug("graph event client did not drain cleanly", exc_info=True)
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)


NULL_NOTIFIER = GraphCommitNotifier()


def notifier_from_config(config: Any) -> GraphCommitNotifier:
    """The configured notifier, or the null one — which is the default.

    Tied to `sync.queue`: the broker it publishes on is the broker the index
    queue already runs, and a region with the queue off has no broker to
    announce anything over. That is not a gap: such a region is one process,
    which reloads its own graph in-process.
    """

    settings = getattr(getattr(config, "sync", None), "queue", None)
    if settings is None or not getattr(settings, "enabled", False):
        return NULL_NOTIFIER
    if str(getattr(settings, "backend", "nats") or "nats").lower() != "nats":
        return NULL_NOTIFIER
    try:
        return NatsGraphNotifier(
            list(getattr(settings, "nats_servers", None) or ["nats://127.0.0.1:4222"]),
            subject=str(getattr(settings, "nats_graph_subject", DEFAULT_SUBJECT)),
        )
    except RuntimeError:
        # The [queue] extra is missing. The queue itself would have refused
        # already; this is a notification, so it degrades to the poll instead.
        logger.warning("graph commit events are unavailable; polling only")
        return NULL_NOTIFIER
