"""Manual ingestion when the process that receives it cannot write.

The reported symptom was a 500 from the UI drop zone on a role-split fleet.
The cause was structural rather than a defect in the upload code: manual
ingestion writes bytes into ``<state_path>/uploads`` before the normal pipeline
can index them, and every non-``all`` role mounts ``/state`` read-only on
purpose, because the indexer is the sole writer of committed state.

So the bytes are forwarded to the tier that *can* write them, which is the same
move the api role already makes for indexing (publish, do not run) and for
graph reads (ask the service). What these assert:

1. Standalone is untouched — no URL configured means the local write, byte for
   byte what a single container always did (rule 7).
2. A forwarding zone really does cross a process boundary, and reports the
   *writing* process's path rather than one predicted from its own mounts.
3. The endpoint is its own trust boundary, and a caller that cannot write must
   not be able to escape the source directory through the relative path.
4. A process that writes its own landing zone never forwards — otherwise a
   tier pointed at its own Service forwards in a circle.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.ingestion.landing_service import (
    LandingServiceClient,
    LandingServiceError,
    LocalLandingZone,
    RemoteLandingZone,
    landing_zone_for_config,
)

TOKEN = "landing-token-for-tests"


# --------------------------------------------------------------- zone choice


def test_no_configured_url_keeps_the_local_write(loaded_config: Any) -> None:
    """Rule 7: a region that configures nothing behaves as it always has."""

    assert loaded_config.ingestion.landing_service_url is None
    assert isinstance(landing_zone_for_config(loaded_config), LocalLandingZone)


def test_a_configured_url_forwards(loaded_config: Any) -> None:
    loaded_config.ingestion.landing_service_url = "http://indexer:8765"
    assert isinstance(landing_zone_for_config(loaded_config), RemoteLandingZone)


def test_the_writing_tier_never_forwards_to_itself(loaded_config: Any) -> None:
    """The guard that stops a circle.

    Every tier reads the same config file, so the indexer resolves a landing
    URL too — pointed at its own Service. ``force_local`` is what makes it
    ignore it, and it is the same escape `graph_for_config` has for the same
    reason.
    """

    loaded_config.ingestion.landing_service_url = "http://indexer:8765"
    zone = landing_zone_for_config(loaded_config, force_local=True)
    assert isinstance(zone, LocalLandingZone)


def test_an_oversized_file_is_refused_before_it_crosses_the_network() -> None:
    """Spending a cluster's bandwidth to be told 'too big' is spending it twice."""

    class Explode:
        def land(self, *_args: Any, **_kwargs: Any) -> dict:
            raise AssertionError("the bytes must not leave this process")

    zone = RemoteLandingZone(Explode())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="over the"):
        zone.write("uploads", "big.md", b"x" * 2048, unique=True, max_bytes=1024)
    with pytest.raises(ValueError, match="empty"):
        zone.write("uploads", "empty.md", b"", unique=True, max_bytes=None)


# ------------------------------------------------------- the real two-process hop


class _Server:
    """The writing tier, on a real loopback port.

    A `TestClient` cannot stand in here: the client under test is a urllib
    client, and the whole point of the change is that a real socket separates
    the process that accepts an upload from the process that writes it. Bound
    to 127.0.0.1, so the suite stays offline.
    """

    def __init__(self, app: Any) -> None:
        import uvicorn

        self.config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self.server.started and self.server.servers:
                sockets = self.server.servers[0].sockets
                if sockets:
                    return f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
            time.sleep(0.02)
        raise RuntimeError("the landing service did not start")

    def __exit__(self, *_exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=30)


@pytest.fixture()
def writing_tier(loaded_config: Any, config_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """An `indexer`-shaped process serving the landing endpoint."""

    monkeypatch.setenv("PHEASANT_INGESTION_SERVICE_TOKEN", TOKEN)
    loaded_config.ingestion.landing_service_token_env = "PHEASANT_INGESTION_SERVICE_TOKEN"
    app = create_app(config=loaded_config, config_path=config_path)
    with _Server(app) as base_url:
        yield base_url


#: The client reads its token from a *different* variable than the server
#: validates against. Both halves run in one pytest process here, so pointing
#: them at one name would mean a test sending a wrong token had also changed
#: what the server expects — and the 401 test would pass by agreeing with
#: itself. Two names is what makes the mismatch real.
CLIENT_TOKEN_ENV = "PHEASANT_TEST_LANDING_CLIENT_TOKEN"


@pytest.fixture(autouse=True)
def _clean_client_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLIENT_TOKEN_ENV, raising=False)


def _client(base_url: str, token: str = TOKEN) -> LandingServiceClient:
    import os

    os.environ[CLIENT_TOKEN_ENV] = token
    return LandingServiceClient(base_url, CLIENT_TOKEN_ENV, timeout_seconds=30.0)


def test_a_forwarded_upload_is_written_by_the_far_side(writing_tier: str, state_path: Path) -> None:
    """The bytes cross the boundary and land where the *writer* says they did."""

    zone = RemoteLandingZone(_client(writing_tier))
    placement = zone.write("uploads", "notes.md", b"# Notes\n\nProse.\n", unique=True)

    landed = Path(placement.stored.path)
    assert landed.read_bytes() == b"# Notes\n\nProse.\n"
    # Reported from the writing process, not predicted from this one's mounts.
    # Both are the same directory here because one machine is running both
    # halves; what matters is that the value came back over the wire.
    assert Path(placement.directory) == state_path / "uploads" / "uploads"
    assert landed.parent == Path(placement.directory)


def test_the_far_side_de_duplicates_names_when_asked(writing_tier: str) -> None:
    """`unique=True` is the drop zone's semantics: two `notes.md` are two files.

    The submission path asks for the opposite, and that difference is the
    entire reason the flag exists rather than being decided in one place.
    """

    zone = RemoteLandingZone(_client(writing_tier))
    first = zone.write("uploads", "notes.md", b"one", unique=True)
    second = zone.write("uploads", "notes.md", b"two", unique=True)

    assert first.stored.path != second.stored.path
    assert Path(first.stored.path).read_bytes() == b"one"
    assert Path(second.stored.path).read_bytes() == b"two"

    # ...and `unique=False` overwrites in place, which is what makes a retry
    # carrying one idempotency key produce one file.
    third = zone.write("uploads", "fixed.md", b"first", unique=False)
    fourth = zone.write("uploads", "fixed.md", b"second", unique=False)
    assert third.stored.path == fourth.stored.path
    assert Path(fourth.stored.path).read_bytes() == b"second"


def test_the_landing_endpoint_is_its_own_trust_boundary(writing_tier: str) -> None:
    """A wrong token is a 401, and `/internal/*` is exempt from the API token
    structurally — so this is the only thing standing in front of a write."""

    with pytest.raises(LandingServiceError):
        RemoteLandingZone(_client(writing_tier, token="not-the-token")).write(
            "uploads", "x.md", b"x", unique=True
        )


@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\evil.dll",
        "/absolute/escape.md",
        "nested/../../../../out.md",
    ],
)
def test_a_hostile_relative_path_cannot_escape_the_source_directory(
    writing_tier: str, hostile: str
) -> None:
    """Re-sanitised on the side that touches the filesystem.

    The caller already cleans the name, and that is not the argument: the name
    arrives at this endpoint from *another process*, so a caller holding the
    landing token must still not be able to choose an arbitrary path. Cleaned
    where the write happens, which is the one place it cannot be skipped.
    """

    zone = RemoteLandingZone(_client(writing_tier))
    placement = zone.write("uploads", hostile, b"payload", unique=False)

    landed = Path(placement.stored.path).resolve()
    assert landed.is_relative_to(Path(placement.directory).resolve())
    assert landed.read_bytes() == b"payload"


def test_a_source_name_cannot_escape_either(writing_tier: str, state_path: Path) -> None:
    zone = RemoteLandingZone(_client(writing_tier))
    placement = zone.write("../../escape", "x.md", b"x", unique=True)
    assert Path(placement.directory).resolve().is_relative_to((state_path / "uploads").resolve())


def test_resolving_a_directory_writes_nothing(writing_tier: str, state_path: Path) -> None:
    """`GET` answers where a source *would* land.

    The drop zone needs this before it has any bytes: it registers the source
    against the writing process's path, and guessing that from its own mounts
    is how two tiers end up disagreeing about where a document is.
    """

    zone = RemoteLandingZone(_client(writing_tier))
    directory = Path(zone.directory("brand-new"))
    assert directory == state_path / "uploads" / "brand-new"
    assert list(directory.iterdir()) == []


def test_the_far_side_enforces_the_size_limit_itself(writing_tier: str, loaded_config: Any) -> None:
    """A server that trusts the client's check is not checking.

    `RemoteLandingZone` refuses an oversized file before it crosses the
    network, which is the right thing for bandwidth and the wrong thing to
    *rely* on: the caller holding the landing token is the one being
    constrained. Going straight to the client bypasses the near-side check,
    which is exactly what a caller that skipped it would do.
    """

    limit_mb = loaded_config.sync.limits.max_file_size_mb or 100
    oversized = b"x" * ((limit_mb * 1024 * 1024) + 1024)

    with pytest.raises(LandingServiceError, match="per-file limit"):
        _client(writing_tier).land("uploads", "huge.md", oversized, unique=False)
    with pytest.raises(LandingServiceError, match="empty"):
        _client(writing_tier).land("uploads", "empty.md", b"", unique=False)


def test_the_service_layer_forwards_too_not_just_the_drop_zone(loaded_config: Any) -> None:
    """`ingest_submit` lands bytes exactly like the drop zone does.

    The first version of this change rewired the drop-zone *route* and left
    both `ServiceContext`s without a zone, so the shared submission path fell
    back to a local write and would have gone on failing on the very tier the
    change was for — on HTTP and MCP at once. That is the "one operation, two
    behaviours" split this repo has paid for repeatedly, so the fallback is
    asserted rather than assumed.
    """

    from pheasant.services import ServiceContext

    loaded_config.ingestion.landing_service_url = "http://indexer:8765"
    context = ServiceContext(config=loaded_config, state=None, searcher=None)
    # No zone passed: the fallback must still forward, because a caller that
    # cannot say which tier it is on is better off making a hop than writing
    # to a filesystem it may not be allowed to write.
    assert isinstance(context.landing_zone(), RemoteLandingZone)

    # ...and an explicitly passed zone always wins over the fallback.
    local = LocalLandingZone(loaded_config.pheasant.state_path)
    assert (
        ServiceContext(
            config=loaded_config, state=None, searcher=None, landing=local
        ).landing_zone()
        is local
    )


def test_an_unreachable_landing_service_is_not_a_silent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CLIENT_TOKEN_ENV, TOKEN)
    # Port 1 on loopback: nothing listens, and the connection is refused
    # immediately rather than hanging until a timeout.
    zone = RemoteLandingZone(LandingServiceClient("http://127.0.0.1:1", CLIENT_TOKEN_ENV, 2.0, 0))
    with pytest.raises(LandingServiceError):
        zone.write("uploads", "x.md", b"x", unique=True)


# ------------------------------------------------------------ the drop zone itself


def test_the_drop_zone_forwards_end_to_end(
    writing_tier: str,
    loaded_config: Any,
    config_path: Path,
    state_path: Path,
    tmp_path: Path,
) -> None:
    """The reported bug, from the surface it was reported on.

    A serving process that cannot write its own landing zone still accepts a
    drop, because the write happens on the tier that can. This is the whole
    change in one assertion.
    """

    import copy
    import io

    # The api replica gets its **own** state directory, which is what two
    # processes with two mounts actually have. Proving the forward by location
    # rather than by sabotage is the only honest option in-process anyway: both
    # halves run here, so patching the filesystem to be read-only would break
    # the writing tier too — which is exactly what the first version of this
    # test did, and it failed with the writer's own refusal.
    serving = copy.deepcopy(loaded_config)
    serving.pheasant.state_path = tmp_path / "api-state"
    serving.ingestion.landing_service_url = writing_tier
    serving.ingestion.landing_service_token_env = "PHEASANT_INGESTION_SERVICE_TOKEN"
    # Two role invariants that have nothing to do with landing and everything
    # to do with this being a realistic api replica: `api` publishes index work
    # rather than running it, so it refuses to start without a queue for an
    # indexer to drain; and a serving role on a routable bind refuses to start
    # unauthenticated.
    serving.sync.queue.enabled = True
    serving.security.api_auth.behind_authenticating_proxy = True
    client = TestClient(create_app(config=serving, config_path=config_path, role="api"))

    response = client.post(
        "/sources/upload",
        files=[("files", ("dropped.md", io.BytesIO(b"# Dropped\n\nProse.\n"), "text/markdown"))],
        data={"source_name": "uploads", "sync_now": "false", "wait": "true"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["filename"] for item in body["stored"]] == ["dropped.md"]

    # Written by the *other* process, into its own state directory...
    landed = state_path / "uploads" / "uploads" / "dropped.md"
    assert landed.read_bytes() == b"# Dropped\n\nProse.\n"
    # ...and not by this one, which is the whole claim.
    assert not (tmp_path / "api-state" / "uploads" / "uploads" / "dropped.md").exists()
    # The source registered points at the writer's path, which is what the
    # indexer will read when the sync runs. Predicting it from the serving
    # replica's own mounts would have named a directory that tier cannot see.
    assert body["path"] == str(state_path / "uploads" / "uploads")
