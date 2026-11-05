"""Searching by when a document was last updated.

Two clocks per document, both on ``artifacts``: ``source_modified_at`` (what
the source of truth says -- a file's mtime, a repository's last commit, Drive's
``modifiedTime``, a page's ``Last-Modified``) and ``ingested_at`` (when this
region took in the content it holds now). A window over either narrows every
arm and both surfaces identically.

The properties held here, each one a way the feature would quietly lie:

* every connector's spelling lands in one sortable form, and an unreadable or
  absent date is *unknown*, never "now";
* a date that moves without the bytes moving is followed without re-indexing,
  and an unchanged corpus writes nothing;
* ``ingested_at`` survives a re-index of identical bytes, a full sync included;
* a repository is dated by git, not by its checkout, and a shallow clone's
  boundary dates nothing;
* an undated document is outside every window, and the response says so;
* a search that asks for no window returns exactly what it did before.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import PheasantConfig
from pheasant.ingestion.timestamps import InvalidDateBound, normalize_timestamp, parse_bound
from pheasant.mcp_server.tools import PheasantTools
from pheasant.search.criteria import DateWindow, InvalidDateWindow
from pheasant.services.errors import InvalidDateRange, ServiceError

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025-03-01T09:12:44Z", "2025-03-01T09:12:44Z"),
        ("2025-03-01T09:12:44.512Z", "2025-03-01T09:12:44Z"),  # Drive
        ("2025-03-01T10:12:44+01:00", "2025-03-01T09:12:44Z"),
        ("2025-03-01 09:12:44", "2025-03-01T09:12:44Z"),
        ("Sat, 01 Mar 2025 09:12:44 GMT", "2025-03-01T09:12:44Z"),  # HTTP
        ("Saturday, 01-Mar-25 09:12:44 GMT", "2025-03-01T09:12:44Z"),  # RFC 850
        (1740820364, "2025-03-01T09:12:44Z"),
        (1740820364000, "2025-03-01T09:12:44Z"),  # epoch milliseconds
        ("1740820364", "2025-03-01T09:12:44Z"),
        (datetime(2025, 3, 1, 9, 12, 44, tzinfo=UTC), "2025-03-01T09:12:44Z"),
    ],
)
def test_every_spelling_lands_in_one_sortable_form(raw: Any, expected: str) -> None:
    assert normalize_timestamp(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "yesterday", "not a date", 0, "0", True, "1969-12-31"])
def test_an_unreadable_or_implausible_date_is_unknown(raw: Any) -> None:
    """Unknown, never a guess: a zero epoch would date a file to 1970."""

    assert normalize_timestamp(raw) is None


def test_canonical_strings_sort_as_time_does() -> None:
    values = ["Sat, 01 Mar 2025 09:12:44 GMT", "2024-12-31T23:59:59.999Z", 1740820365]
    ordered = sorted(normalize_timestamp(v) for v in values)
    assert ordered == ["2024-12-31T23:59:59Z", "2025-03-01T09:12:44Z", "2025-03-01T09:12:45Z"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025", "2025-01-01T00:00:00Z"),
        ("2025-03", "2025-03-01T00:00:00Z"),
        ("2025-03-07", "2025-03-07T00:00:00Z"),
        ("2025-03-07T12:00:00+02:00", "2025-03-07T10:00:00Z"),
        (None, None),
        ("  ", None),
    ],
)
def test_a_bound_names_the_start_of_the_period(raw: Any, expected: str | None) -> None:
    assert parse_bound(raw) == expected


def test_an_unreadable_bound_is_refused_by_name() -> None:
    with pytest.raises(InvalidDateBound, match="updated_after"):
        parse_bound("last tuesday", name="updated_after")
    with pytest.raises(InvalidDateBound, match="calendar"):
        parse_bound("2025-02-30")


def test_a_window_refuses_what_it_cannot_apply() -> None:
    assert DateWindow.parse() is None
    with pytest.raises(InvalidDateWindow, match="date_field"):
        DateWindow.parse("2025", None, "created")
    with pytest.raises(InvalidDateWindow, match="earlier than"):
        DateWindow.parse("2025-06", "2025-01")
    window = DateWindow.parse("2025", "2026", "ingested")
    assert window is not None and window.column == "ingested_at"
    # Inclusive start, exclusive end: adjacent windows never share a document.
    assert window.admits_date("2025-01-01T00:00:00Z")
    assert not window.admits_date("2026-01-01T00:00:00Z")
    assert not window.admits_date(None)


# ---------------------------------------------------------------------------
# A folder region whose files carry known dates
# ---------------------------------------------------------------------------

CORPUS = {
    # path: (text, source mtime)
    "old/rotation.md": (
        "# Rotation runbook 2023\n\nCredential rotation used the legacy vault.\n",
        "2023-05-10T08:00:00Z",
    ),
    "mid/rotation.md": (
        "# Rotation runbook 2024\n\nCredential rotation moved to the gateway.\n",
        "2024-06-15T08:00:00Z",
    ),
    "new/rotation.md": (
        "# Rotation runbook 2025\n\nCredential rotation is automatic now.\n",
        "2025-02-20T08:00:00Z",
    ),
}


def _stamp(path: Path, iso: str) -> None:
    epoch = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    os.utime(path, (epoch, epoch))


def _build(root: Path, storage: dict[str, Any] | None = None) -> dict[str, Any]:
    workspace = root / "workspace"
    for relative, (text, when) in CORPUS.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _stamp(path, when)
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "dated",
                "defaults": "standalone",
                "state_path": str(root / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(root / "exports"),
            },
            "server": {"host": "127.0.0.1"},
            "storage": {"graph_snapshots": False, **(storage or {})},
            "sync": {"watcher": {"enabled": False}, "scheduler": {"enabled": False}},
            "sources": [
                {
                    "name": "docs",
                    "type": "markdown_folder",
                    "path": str(workspace),
                    "include": ["**/*.md"],
                }
            ],
        }
    )
    tools = PheasantTools(config)
    tools.engine.sync_source("docs", "full")
    tools.engine.reload_graph()
    client = TestClient(create_app(config, config_path=str(root / "pheasant.yaml")))
    return {
        "config": config,
        "tools": tools,
        "client": client,
        "kb": config.knowledge_base_id,
        "workspace": workspace,
    }


@pytest.fixture(scope="module")
def region(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _build(tmp_path_factory.mktemp("dated"))


@pytest.fixture
def mutable(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _build(tmp_path_factory.mktemp("dated-mutable"))


def _dates(region: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    rows = region["tools"].engine.state.rows(
        "SELECT relative_path, source_modified_at, ingested_at FROM artifacts"
    )
    return {str(r["relative_path"]): (r["source_modified_at"], r["ingested_at"]) for r in rows}


def _paths(payload: dict[str, Any]) -> set[str]:
    return {
        str((hit.get("provenance") or {}).get("relative_path") or hit.get("relative_path") or "")
        for hit in payload["results"]
    } - {""}


def _both(region: dict[str, Any], **criteria: Any) -> tuple[dict, dict]:
    over_http = region["client"].post(
        "/search",
        json={"query": "credential rotation", "max_results": 10, **criteria},
    )
    assert over_http.status_code == 200, over_http.text
    over_mcp = region["tools"].search_context(
        region["kb"], "credential rotation", "hybrid", 10, **criteria
    )
    return over_http.json(), over_mcp


def test_the_source_date_is_stored_in_canonical_form(region: dict[str, Any]) -> None:
    dates = _dates(region)
    assert {path: dates[path][0] for path in CORPUS} == {
        path: when for path, (_text, when) in CORPUS.items()
    }
    assert all(ingested for _source, ingested in dates.values())


@pytest.mark.parametrize(
    ("criteria", "expected"),
    [
        ({"updated_after": "2024"}, {"mid/rotation.md", "new/rotation.md"}),
        ({"updated_before": "2024"}, {"old/rotation.md"}),
        ({"updated_after": "2024-01", "updated_before": "2025"}, {"mid/rotation.md"}),
        ({"updated_after": "2025-02-20T08:00:00Z"}, {"new/rotation.md"}),  # inclusive start
        ({"updated_before": "2025-02-20T08:00:00Z", "updated_after": "2025"}, set()),
    ],
)
def test_a_window_narrows_identically_on_both_surfaces(
    region: dict[str, Any], criteria: dict[str, Any], expected: set[str]
) -> None:
    over_http, over_mcp = _both(region, **criteria)
    assert _paths(over_http) == _paths(over_mcp) == expected
    assert over_http["date_window"] == over_mcp["date_window"]
    assert over_http["date_window"]["field"] == "source"


def test_a_windowed_hit_carries_its_documents_dates(region: dict[str, Any]) -> None:
    _http, over_mcp = _both(region, updated_after="2025")
    (hit,) = [h for h in over_mcp["results"] if _paths({"results": [h]}) == {"new/rotation.md"}][:1]
    assert hit["provenance"]["source_modified_at"] == "2025-02-20T08:00:00Z"
    assert hit["provenance"]["ingested_at"]


def test_the_ingested_clock_is_a_different_question(region: dict[str, Any]) -> None:
    """Every file was ingested just now, whatever its source says."""

    over_http, over_mcp = _both(region, updated_before="2025", date_field="ingested")
    assert _paths(over_http) == _paths(over_mcp) == set()
    over_http, over_mcp = _both(region, updated_after="2025", date_field="ingested")
    assert _paths(over_http) == _paths(over_mcp) == set(CORPUS)


def test_no_window_returns_the_payload_it_always_did(region: dict[str, Any]) -> None:
    _http, plain = _both(region)
    assert "date_window" not in plain
    for hit in plain["results"]:
        assert "source_modified_at" not in (hit.get("provenance") or {})


@pytest.mark.parametrize(
    ("criteria", "fragment"),
    [
        ({"updated_after": "soon"}, "updated_after"),
        ({"updated_after": "2025", "updated_before": "2024"}, "earlier than"),
        ({"updated_after": "2025", "date_field": "created"}, "date_field"),
    ],
)
def test_a_bad_window_is_refused_with_one_text(
    region: dict[str, Any], criteria: dict[str, Any], fragment: str
) -> None:
    response = region["client"].post("/search", json={"query": "rotation", **criteria})
    with pytest.raises(ServiceError) as refused:
        region["tools"].search_context(region["kb"], "rotation", "hybrid", 5, **criteria)
    assert isinstance(refused.value, InvalidDateRange)
    assert response.status_code == 422  # every search refusal's status
    assert response.json()["detail"] == str(refused.value)
    assert response.json()["code"] == refused.value.code == "INVALID_DATE_RANGE"
    assert fragment in str(refused.value)


def test_the_window_is_part_of_the_query_identity(region: dict[str, Any]) -> None:
    _h, plain = _both(region)
    _h, windowed = _both(region, updated_after="2024")
    assert plain["lineage"]["query_id"] != windowed["lineage"]["query_id"]


def test_each_arm_is_narrowed(region: dict[str, Any]) -> None:
    """Not only the text arm: the graph arm is filtered against the same rows."""

    for mode in ("text", "graph"):
        payload = region["tools"].search_context(
            region["kb"], "rotation runbook", mode, 20, updated_before="2024"
        )
        assert _paths(payload) <= {"old/rotation.md"}, mode


# ---------------------------------------------------------------------------
# Keeping the dates true without re-indexing
# ---------------------------------------------------------------------------


def test_a_touched_file_is_redated_without_being_reindexed(mutable: dict[str, Any]) -> None:
    before = _dates(mutable)
    _stamp(mutable["workspace"] / "old/rotation.md", "2025-09-01T00:00:00Z")
    result = mutable["tools"].engine.sync_source("docs", "incremental")
    after = _dates(mutable)
    assert after["old/rotation.md"][0] == "2025-09-01T00:00:00Z"
    # The bytes did not change, so neither did the ingestion clock.
    assert after["old/rotation.md"][1] == before["old/rotation.md"][1]
    assert result.indexed_artifacts == 0
    assert result.details.get("redated_artifacts") == 1


def test_an_unchanged_corpus_redates_nothing(mutable: dict[str, Any]) -> None:
    before = _dates(mutable)
    result = mutable["tools"].engine.sync_source("docs", "incremental")
    assert _dates(mutable) == before
    assert "redated_artifacts" not in result.details


def test_a_full_sync_keeps_when_identical_bytes_were_ingested(mutable: dict[str, Any]) -> None:
    state = mutable["tools"].engine.state
    state.execute("UPDATE artifacts SET ingested_at='2020-01-01T00:00:00Z'")
    edited = mutable["workspace"] / "new/rotation.md"
    edited.write_text("# Rotation runbook 2025\n\nRotation is automatic and audited.\n")
    _stamp(edited, "2025-03-01T00:00:00Z")
    mutable["tools"].engine.sync_source("docs", "full")
    dates = _dates(mutable)
    assert dates["old/rotation.md"][1] == "2020-01-01T00:00:00Z"
    assert dates["mid/rotation.md"][1] == "2020-01-01T00:00:00Z"
    assert dates["new/rotation.md"][1] > "2020-01-01T00:00:00Z"
    assert dates["new/rotation.md"][0] == "2025-03-01T00:00:00Z"


def test_an_edit_moves_both_clocks(mutable: dict[str, Any]) -> None:
    state = mutable["tools"].engine.state
    state.execute("UPDATE artifacts SET ingested_at='2020-01-01T00:00:00Z'")
    edited = mutable["workspace"] / "mid/rotation.md"
    edited.write_text("# Rotation runbook 2024\n\nRevised.\n")
    _stamp(edited, "2025-04-01T00:00:00Z")
    mutable["tools"].engine.sync_source("docs", "incremental")
    dates = _dates(mutable)
    assert dates["mid/rotation.md"][0] == "2025-04-01T00:00:00Z"
    assert dates["mid/rotation.md"][1] > "2020-01-01T00:00:00Z"
    assert dates["old/rotation.md"][1] == "2020-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# Connectors that report dates their own way
# ---------------------------------------------------------------------------


def test_a_page_without_last_modified_is_undated_not_dated_now() -> None:
    from pheasant.config.schema import SourceConfig, SourceType
    from pheasant.ingestion.pipeline import parse_connector_payload
    from pheasant.sync.connectors import ConnectorItem, ConnectorPayload

    source = SourceConfig(name="site", type=SourceType.web_collection, path=Path("/unused"))
    item = ConnectorItem(identity="web:site:a", relative_path="a.md", uri="https://x/a.md")
    dated = parse_connector_payload(
        source,
        item,
        ConnectorPayload(item=item, content=b"# A\n", mtime="Sat, 01 Mar 2025 09:12:44 GMT"),
    )
    undated = parse_connector_payload(
        source, item, ConnectorPayload(item=item, content=b"# A\n", mtime=None)
    )
    assert dated is not None and dated.source_modified_at == "2025-03-01T09:12:44Z"
    assert undated is not None and undated.source_modified_at is None
    assert undated.mtime  # `mtime` keeps its old fallback; only the new field refuses to guess


def test_the_date_crosses_the_worker_wire_and_tolerates_an_older_worker() -> None:
    from pheasant.ingestion.pipeline import ParsedArtifact
    from pheasant.sync.remote_worker import parsed_from_wire, parsed_to_wire

    parsed = ParsedArtifact(
        id="file:s:a.txt:branch=none",
        source_id="s",
        path="a.txt",
        relative_path="a.txt",
        type="text",
        mime_type=None,
        size_bytes=1,
        sha256="x",
        mtime="2025-01-01T00:00:00Z",
        git_branch=None,
        git_commit=None,
        chunks=[],
        source_modified_at="2025-01-01T00:00:00Z",
    )
    wire = parsed_to_wire(parsed)
    assert parsed_from_wire(wire).source_modified_at == "2025-01-01T00:00:00Z"
    wire.pop("source_modified_at")
    assert parsed_from_wire(wire).source_modified_at is None


# ---------------------------------------------------------------------------
# Repositories are dated by git
# ---------------------------------------------------------------------------

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo: Path, *args: str, when: str | None = None) -> str:
    env = {**os.environ, **GIT_ENV}
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout


def _commit(repo: Path, files: dict[str, str], when: str) -> None:
    for relative, text in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"at {when}", when=when)


def _repo_region(root: Path, repo: Path) -> PheasantTools:
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "repo",
                "defaults": "standalone",
                "state_path": str(root / "state"),
                "workspace_root": str(root),
                "exports_path": str(root / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": [
                {"name": "code", "type": "repository", "path": str(repo), "include": ["**/*.md"]}
            ],
        }
    )
    return PheasantTools(config)


def _repo_dates(tools: PheasantTools) -> dict[str, Any]:
    return {
        str(r["relative_path"]): r["source_modified_at"]
        for r in tools.engine.state.rows("SELECT relative_path, source_modified_at FROM artifacts")
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, {"a.md": "# A\n", "b.md": "# B\n"}, "2023-01-01T00:00:00Z")
    _commit(repo, {"b.md": "# B\n\nedited\n"}, "2024-01-01T00:00:00Z")
    return repo


def test_a_repository_is_dated_by_its_last_commit_not_its_checkout(
    tmp_path: Path, repo: Path
) -> None:
    tools = _repo_region(tmp_path, repo)
    tools.engine.sync_source("code", "full")
    assert _repo_dates(tools) == {
        "a.md": "2023-01-01T00:00:00Z",
        "b.md": "2024-01-01T00:00:00Z",
    }
    # A new commit is dated by a diff, not a re-walk; a local edit by its mtime.
    _commit(repo, {"a.md": "# A\n\nnew\n"}, "2025-01-01T00:00:00Z")
    (repo / "b.md").write_text("# B\n\nuncommitted\n")
    _stamp(repo / "b.md", "2025-06-01T00:00:00Z")
    tools.engine.sync_source("code", "incremental")
    assert _repo_dates(tools) == {
        "a.md": "2025-01-01T00:00:00Z",
        "b.md": "2025-06-01T00:00:00Z",
    }


def test_a_shallow_clones_boundary_dates_nothing(tmp_path: Path, repo: Path) -> None:
    _commit(repo, {"c.md": "# C\n"}, "2025-01-01T00:00:00Z")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(clone)],
        check=True,
        capture_output=True,
    )
    tools = _repo_region(tmp_path, clone)
    tools.engine.sync_source("code", "full")
    # Only c.md changed in the one commit the clone holds -- but the boundary
    # commit lists every file, so all three are undated rather than misdated.
    assert _repo_dates(tools) == {"a.md": None, "b.md": None, "c.md": None}


# ---------------------------------------------------------------------------
# Upgrading a region that predates the columns
# ---------------------------------------------------------------------------


def test_an_old_state_gains_the_columns_and_is_backfilled(tmp_path: Path) -> None:
    from pheasant.persistence.state_store import StateStore

    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE artifacts (
          id TEXT PRIMARY KEY, source_id TEXT NOT NULL, type TEXT NOT NULL,
          path TEXT NOT NULL, relative_path TEXT, mime_type TEXT, size_bytes INTEGER,
          sha256 TEXT, mtime TEXT, git_branch TEXT, git_commit TEXT,
          last_indexed_at TEXT, status TEXT
        );
        INSERT INTO artifacts(id, source_id, type, path, relative_path, last_indexed_at)
        VALUES ('file:s:a:branch=none', 's', 'text', 'a', 'a', '2024-02-02T00:00:00Z');
        """
    )
    conn.commit()
    conn.close()
    store = StateStore(db)
    store.migrate()
    store.migrate()  # idempotent
    (row,) = store.rows("SELECT source_modified_at, ingested_at FROM artifacts")
    assert row["source_modified_at"] is None
    assert row["ingested_at"] == "2024-02-02T00:00:00Z"


def test_the_vector_arm_is_narrowed_by_its_documents_date(tmp_path: Path) -> None:
    """ "car" reaches vehicles.md through the vector arm alone (planted synonym)."""

    from pheasant.search.criteria import DateWindow
    from pheasant.search.hybrid import HybridSearch
    from pheasant.search.sqlite_store import SearchStore
    from tests.conftest import make_vector_engine

    engine = make_vector_engine(tmp_path)
    notes = tmp_path / "vector-workspace" / "notes"
    _stamp(notes / "vehicles.md", "2023-03-03T00:00:00Z")
    engine.sync_source("notes", "full")
    searcher = HybridSearch(SearchStore(engine.state), vector=engine.vector_searcher())

    def vector_paths(window: DateWindow | None) -> set[str]:
        payload = searcher.search_context(
            engine.config.knowledge_base_id,
            "car",
            mode="vector",
            max_results=5,
            **({"window": window} if window else {}),
        )
        return _paths(payload)

    assert any(path.endswith("vehicles.md") for path in vector_paths(None))
    kept = vector_paths(DateWindow.parse(None, "2024"))
    assert any(path.endswith("vehicles.md") for path in kept)
    assert not any(path.endswith("vehicles.md") for path in vector_paths(DateWindow.parse("2024")))


# ---------------------------------------------------------------------------
# Asking a question about a period
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "since", "before", "text"),
    [
        ("@since 2025-01 what changed", "2025-01", None, "what changed"),
        ("how was it done @since 2024 @before 2025", "2024", "2025", "how was it done"),
        ("@before 2024-07-01T00:00:00Z rotation?", None, "2024-07-01T00:00:00Z", "rotation?"),
        ("@after 2023 @until 2024 rotation", "2023", "2024", "rotation"),
        # Strict: a word that is not a date stays in the question.
        ("what happens @before lunch", None, None, "what happens @before lunch"),
    ],
)
def test_since_and_before_are_read_anywhere(
    question: str, since: str | None, before: str | None, text: str
) -> None:
    from pheasant.assistant import keywords

    read = keywords.read(question)
    assert (read.since, read.before) == (since, before)
    assert read.text == text


def _ask(region: dict[str, Any], question: str, **criteria: Any) -> tuple[dict, dict]:
    over_http = region["client"].post(
        "/assistant/chat", json={"question": question, "workflow": "default", **criteria}
    )
    assert over_http.status_code == 200, over_http.text
    over_mcp = region["tools"].ask_knowledge_base(
        region["kb"], question, workflow="default", **criteria
    )
    return over_http.json(), over_mcp


def _cited(payload: dict[str, Any]) -> set[str]:
    return {str(c.get("relative_path") or "") for c in payload.get("citations") or []} - {""}


def test_an_answer_is_drawn_only_from_the_window(region: dict[str, Any]) -> None:
    over_http, over_mcp = _ask(
        region, "how is credential rotation done", updated_after="2024", updated_before="2025"
    )
    assert _cited(over_http) == _cited(over_mcp) == {"mid/rotation.md"}
    assert over_http["date_window"] == over_mcp["date_window"]
    assert all(c.get("source_modified_at") for c in over_mcp["citations"])


def test_the_keywords_say_the_same_as_the_request(region: dict[str, Any]) -> None:
    over_http, over_mcp = _ask(region, "@since 2025 how is credential rotation done")
    assert _cited(over_http) == _cited(over_mcp) == {"new/rotation.md"}
    assert over_mcp["date_window"]["updated_after"] == "2025-01-01T00:00:00Z"
    # The keyword wins over the request's own bound.
    _http, pinned = _ask(region, "@before 2024 rotation runbook", updated_before="2026")
    assert _cited(pinned) == {"old/rotation.md"}


def test_an_unwindowed_answer_is_unchanged(region: dict[str, Any]) -> None:
    _http, plain = _ask(region, "how is credential rotation done")
    assert "date_window" not in plain
    assert not any("source_modified_at" in c for c in plain.get("citations") or [])


def test_a_bad_keyword_date_is_refused(region: dict[str, Any]) -> None:
    with pytest.raises(ServiceError) as refused:
        region["tools"].ask_knowledge_base(
            region["kb"], "@since 2025-02-30 rotation", workflow="default"
        )
    assert refused.value.code == "INVALID_DATE_RANGE"


def test_the_prompt_names_a_documents_date_only_under_a_window() -> None:
    from pheasant.assistant.chat import build_prompt

    base = {"index": 1, "title": "a.md", "relative_path": "a.md", "snippet": "x"}
    dated = build_prompt("q", [{**base, "source_modified_at": "2024-06-15T08:00:00Z"}], [])
    plain = build_prompt("q", [base], [])
    assert "last updated 2024-06-15" in dated
    assert "last updated" not in plain


# ---------------------------------------------------------------------------
# The same on Postgres (CI's backend-parity job; skipped offline)
# ---------------------------------------------------------------------------

PG_DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()


@pytest.mark.skipif(not PG_DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run on Postgres")
def test_the_window_holds_on_postgres(tmp_path: Path) -> None:
    """Every dialect branch the window crosses: the upsert, the migration's
    guarded ALTER, the lexical arm (pg_textsearch's fence or tsvector), the
    reconcile pass and the batched lookup."""

    import psycopg

    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    lexical = (
        "pg_textsearch" if os.environ.get("PHEASANT_REQUIRE_PG_TEXTSEARCH") == "1" else "tsvector"
    )
    region = _build(
        tmp_path,
        {"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN", "lexical_index": lexical},
    )
    assert {path: d[0] for path, d in _dates(region).items()} == {
        path: when for path, (_text, when) in CORPUS.items()
    }
    for criteria, expected in (
        ({"updated_after": "2024"}, {"mid/rotation.md", "new/rotation.md"}),
        ({"updated_after": "2024-01", "updated_before": "2025"}, {"mid/rotation.md"}),
    ):
        over_http, over_mcp = _both(region, **criteria)
        assert _paths(over_http) == _paths(over_mcp) == expected, criteria
    _stamp(region["workspace"] / "old/rotation.md", "2025-09-01T00:00:00Z")
    result = region["tools"].engine.sync_source("docs", "incremental")
    assert result.details.get("redated_artifacts") == 1
    assert _dates(region)["old/rotation.md"][0] == "2025-09-01T00:00:00Z"
    _http, answered = _ask(region, "@since 2025-06 credential rotation")
    assert _cited(answered) == {"old/rotation.md"}
