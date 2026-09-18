"""Who writes submitted bytes, when the process that received them cannot.

`landing.py` owns *where* a submitted file goes and what its name is allowed to
be. This module owns *which process performs the write*, and it exists because
those turned out to be different questions in a fleet.

The single container answers both the same way: the process that accepts an
upload also writes it, because it is also the process that indexes. The
role-split fleet cannot. There the tier a browser or an agent can reach is
``api``, and an api replica mounts ``/state`` read-only on purpose — the
indexer is the sole writer of committed state, which is what lets N serving
replicas read a graph one process commits. So manual ingestion had nowhere to
put its bytes and failed at the first ``mkdir`` with an errno, on every surface
that lands a file: the UI drop zone, `ingest_submit`, and the readiness probe's
scratch source.

The fix is the one the api role already uses for everything else it cannot do
itself. An api replica does not index; it *publishes* a sync request and an
indexer runs it. An api replica does not hold the graph; it asks the graph
service. So it does not write the landing zone either — it forwards the bytes
to the tier that can, over an authenticated internal endpoint, and that tier
performs the identical local write. There is still exactly one implementation
of the write, and still no second ingestion path: what crosses the network is
the bytes, and what happens on the far side is `LocalLandingZone`.

**The shape is deliberately `graph/query_service.py`'s**, down to the selector
function: a small urllib client with a bearer token read from the environment,
a `None` default that keeps standalone in-process, and a ``force_local`` escape
so the process *serving* the endpoint cannot proxy to itself. Two mechanisms
for one idea is a thing this codebase has paid for more than once; this is the
same idea as the graph boundary, so it is the same mechanism.

What deliberately does **not** cross the boundary: receipts, source
registration and the sync request. Those are already state-store writes, and
the state store in a fleet is Postgres — which an api replica can write. Only
the filesystem was ever the problem, so only the filesystem write moves.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from pheasant.ingestion.landing import (
    StoredUpload,
    safe_filename,
    store_upload,
    upload_root,
)
from pheasant.telemetry.interactions import inject_traceparent

#: The endpoint a remote zone posts to. One constant, because the client and
#: the route that serves it are in different layers and a path typed twice is
#: a path that drifts once.
LANDING_PATH = "/internal/ingestion/land"


class LandingServiceError(RuntimeError):
    """The configured landing service could not store a submitted file."""


@dataclass(frozen=True)
class LandingPlacement:
    """Where a file landed, as the caller needs to describe it.

    ``directory`` is the absolute path **on the writing process**, which is
    what a registered source has to point at: the indexer is who reads it
    during the sync. It is returned by the far side rather than predicted
    locally, so the two processes are not required to agree about their mounts
    by coincidence.
    """

    directory: str
    stored: StoredUpload


class LocalLandingZone:
    """Write it here. What every standalone install has always done."""

    def __init__(self, state_path: Path | str) -> None:
        self.state_path = Path(state_path)

    def directory(self, source_name: str) -> str:
        return str(upload_root(self.state_path, source_name))

    def write(
        self,
        source_name: str,
        relative_path: str,
        data: bytes,
        *,
        unique: bool,
        max_bytes: int | None = None,
    ) -> LandingPlacement:
        root = upload_root(self.state_path, source_name)
        if unique:
            return LandingPlacement(
                directory=str(root),
                stored=store_upload(root, relative_path, data, max_bytes=max_bytes),
            )
        # Deterministic placement: a retry carrying one idempotency key means
        # one file, so it lands on the path its first attempt used and the
        # bytes are written over themselves. `landing.safe_filename` is applied
        # per component by the caller; re-applied here by `_sanitised_relative`
        # because this is also the far side of a network boundary.
        relative = _sanitised_relative(relative_path)
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return LandingPlacement(
            directory=str(root),
            stored=StoredUpload(filename=target.name, path=str(target), size_bytes=len(data)),
        )


class RemoteLandingZone:
    """Hand it to the tier that can write, and report what that tier did."""

    def __init__(self, client: LandingServiceClient) -> None:
        self.client = client

    def directory(self, source_name: str) -> str:
        return str(self.client.resolve(source_name))

    def write(
        self,
        source_name: str,
        relative_path: str,
        data: bytes,
        *,
        unique: bool,
        max_bytes: int | None = None,
    ) -> LandingPlacement:
        # Refused here rather than after a round trip. Shipping 200 MB across
        # a cluster network to be told it is over a 100 MB limit spends the
        # bandwidth to reach the same answer, and `store_upload` makes the
        # same check for the same reason: accepting bytes and then deleting
        # them still means something held them.
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError(
                f"{relative_path} is {len(data) // (1024 * 1024)} MB, over the "
                f"{max_bytes // (1024 * 1024)} MB per-file limit (sync.limits.max_file_size_mb)"
            )
        if not data:
            raise ValueError(f"{relative_path} is empty")
        payload = self.client.land(source_name, relative_path, data, unique=unique)
        return LandingPlacement(
            directory=str(payload["directory"]),
            stored=StoredUpload(
                filename=str(payload["filename"]),
                path=str(payload["path"]),
                size_bytes=int(payload["size_bytes"]),
            ),
        )


class LandingServiceClient:
    """Small authenticated client that posts one document body per call.

    One file per request, as raw ``application/octet-stream`` with the naming
    in the query string. The alternatives were worse for the same work: JSON
    with base64 inflates a 100 MB upload to 133 MB to carry bytes that are
    already bytes, and multipart means writing a parser on the far side for a
    payload that is a single blob. Per-file also gives partial failure for
    free, which the drop zone already reports per file.

    Deliberately **not** the graph client's DNS round-robin. That exists
    because the graph tier is N interchangeable replicas; the tier that writes
    the landing zone is the commit authority, of which there is one per shard
    with the rest as hot standbys. Spreading writes across them would be
    answering a question nobody asked.
    """

    def __init__(
        self,
        base_url: str,
        token_env: str,
        timeout_seconds: float = 120.0,
        retries: int = 1,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token_env = token_env
        self.timeout = max(0.1, float(timeout_seconds))
        self.retries = max(0, int(retries))
        # Internal endpoint: bypass any ambient HTTP_PROXY, exactly as the
        # graph client does, so cluster traffic stays inside the cluster.
        self._opener = build_opener(ProxyHandler({}))

    def _token(self) -> str:
        token = os.environ.get(self.token_env or "", "")
        if not token:
            raise LandingServiceError(
                f"The landing service requires a token in environment variable {self.token_env!r}"
            )
        return token

    def resolve(self, source_name: str) -> str:
        """The directory the far side would write this source into."""

        return str(self._call("GET", source_name, None, None)["directory"])

    def land(
        self, source_name: str, relative_path: str, data: bytes, *, unique: bool
    ) -> dict[str, Any]:
        return self._call("POST", source_name, relative_path, data, unique=unique)

    def _call(
        self,
        method: str,
        source_name: str,
        relative_path: str | None,
        data: bytes | None,
        *,
        unique: bool = False,
    ) -> dict[str, Any]:
        query = f"source_name={quote(source_name, safe='')}"
        if relative_path is not None:
            query += f"&relative_path={quote(relative_path, safe='')}"
            query += f"&unique={'true' if unique else 'false'}"
        target = f"{self.base_url}{LANDING_PATH}?{query}"
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            headers = {
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/octet-stream",
                "Accept": "application/json",
            }
            # Carry the caller's trace across the hop, for the same reason the
            # graph boundary does: without it an operator sees "the upload took
            # nine seconds" and cannot see which side of the boundary spent it.
            inject_traceparent(headers)
            request = Request(target, data=data, method=method, headers=headers)
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict) or "directory" not in payload:
                    raise LandingServiceError("landing service returned an invalid response")
                return payload
            except HTTPError as exc:
                detail = _http_detail(exc)
                # A refusal is deterministic — an oversized file, a denylisted
                # path, a landing zone the far side cannot write either.
                # Retrying only delays the useful error, and for a request
                # carrying a document body it re-sends the body to do it.
                if 400 <= exc.code < 500:
                    raise LandingServiceError(detail or exc.reason) from exc
                last = exc
            except (OSError, TimeoutError, URLError, json.JSONDecodeError) as exc:
                last = exc
            if attempt < self.retries:
                time.sleep(0.05 * (attempt + 1))
        raise LandingServiceError(
            f"landing service at {self.base_url!r} could not store {relative_path!r}: {last}"
        ) from last


def _http_detail(exc: HTTPError) -> str | None:
    try:
        return json.loads(exc.read().decode("utf-8")).get("detail")
    except Exception:  # pragma: no cover - malformed upstream response
        return None


def _sanitised_relative(raw: str) -> str:
    """A relative path that cannot leave the source's directory.

    Applied on whichever side is about to touch the filesystem, which in a
    fleet is the far side of a network boundary: the name arrived from another
    process, and a caller that holds the landing token must still not be able
    to write ``../../etc/cron.d/x``. `landing.safe_filename`'s own docstring
    argues a filename should be cleaned once rather than checked twice — this
    is that one cleaning, performed where the write happens.
    """

    raw_parts = str(raw or "").replace("\\", "/").split("/")
    parts = [part for part in raw_parts if part not in ("", ".", "..")]
    if not parts:
        raise ValueError("A submitted item needs a relative path")
    return "/".join(safe_filename(part, fallback="item") for part in parts)


def landing_zone_for_config(
    config: Any,
    *,
    force_local: bool = False,
) -> LocalLandingZone | RemoteLandingZone:
    """The landing zone this process should use, defaulting to local.

    ``force_local`` is how the process that *serves* the endpoint avoids
    proxying to itself — the same guard, for the same reason, as
    `graph.query_service.graph_for_config`. Without it an indexer configured
    with a landing URL pointing at its own Service would forward its writes in
    a circle.
    """

    settings = getattr(config, "ingestion", None)
    url = str(getattr(settings, "landing_service_url", "") or "").strip()
    state_path = Path(getattr(getattr(config, "pheasant", None), "state_path", ".") or ".")
    if force_local or not url:
        return LocalLandingZone(state_path)
    return RemoteLandingZone(
        LandingServiceClient(
            url,
            str(getattr(settings, "landing_service_token_env", "") or ""),
            float(getattr(settings, "landing_service_timeout_seconds", 120.0) or 120.0),
        )
    )


__all__ = [
    "LANDING_PATH",
    "LandingPlacement",
    "LandingServiceClient",
    "LandingServiceError",
    "LocalLandingZone",
    "RemoteLandingZone",
    "landing_zone_for_config",
]
