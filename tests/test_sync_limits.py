"""Guardrails for pointing a source at anything the operator can read.

pheasant deliberately allows a source to name any readable path, so the
traversal itself has to be bounded and the index has to stay free of
credentials. These tests pin that bargain:

* the walk **prunes** — excluded directories are never descended into, which
  is what keeps a home-directory source from costing a full tree walk;
* a source larger than its budget is **refused**, not partially indexed;
* ``scan`` reports the real shape so a depth can be chosen from evidence;
* ``--depth`` / ``full_scan`` are per-run and never persisted;
* credential patterns are unioned into every source's excludes, even when
  the caller supplies its own ``exclude`` list.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.config.schema import SECRET_EXCLUDES, PheasantConfig, SourceConfig, SourceType
from pheasant.ingestion.walk import WalkBudget, should_prune_directory, walk_source
from pheasant.sync.engine import SyncEngine

SECRETS = {
    ".ssh/id_rsa": "SECRETKEY_aaa111",
    ".docker/config.json": '{"auths":{"x":{"auth":"SECRETDOCKER_bbb222"}}}',
    ".config/gh/hosts.yml": "github.com:\n  oauth_token: SECRETGH_ccc333\n",
    ".aws/credentials": "aws_secret_access_key = SECRETAWS_ddd444",
    ".env": "OPENAI_API_KEY=SECRETENV_eee555",
}
SECRET_MARKERS = [
    "SECRETKEY_aaa111",
    "SECRETDOCKER_bbb222",
    "SECRETGH_ccc333",
    "SECRETAWS_ddd444",
    "SECRETENV_eee555",
]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A tree shaped like a home directory: secrets, noise, and real content."""
    root = tmp_path / "home"
    for relative, body in SECRETS.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    (root / "projects" / "app").mkdir(parents=True)
    (root / "projects" / "app" / "README.md").write_text(
        "# App\nordinary project documentation\n", encoding="utf-8"
    )
    # Bulk that must be pruned rather than walked.
    noise = root / "projects" / "app" / "node_modules" / "pkg"
    noise.mkdir(parents=True)
    for index in range(200):
        (noise / f"f{index}.json").write_text("{}", encoding="utf-8")
    # Bulk that is *not* excluded, so budget tests exercise a real overflow
    # rather than accidentally measuring the node_modules prune.
    bulk = root / "projects" / "app" / "data"
    bulk.mkdir(parents=True)
    for index in range(30):
        (bulk / f"row{index}.json").write_text(f'{{"n": {index}}}', encoding="utf-8")
    return root


def _config(tmp_path: Path, root: Path, **overrides) -> PheasantConfig:
    payload = {
        "pheasant": {
            "name": "limits",
            "workspace_root": str(root),
            "state_path": str(tmp_path / "state"),
            "exports_path": str(tmp_path / "exports"),
        },
        "sources": [],
    }
    payload.update(overrides)
    return PheasantConfig.model_validate(payload)


def _client(config: PheasantConfig, tmp_path: Path) -> TestClient:
    path = tmp_path / "pheasant.yaml"
    if not path.exists():
        path.write_text("pheasant:\n  name: limits\n", encoding="utf-8")
    return TestClient(create_app(config, config_path=str(path)))


# ---------------------------------------------------------------------------
# The walk prunes rather than materializing
# ---------------------------------------------------------------------------


def test_excluded_directories_are_never_descended(home: Path) -> None:
    """The whole point: excluding a subtree must cost less, not more."""

    report = walk_source(
        home,
        include=["**/*.md"],
        exclude=["**/node_modules/**"],
        budget=WalkBudget.unlimited(),
    )

    assert report.directories_pruned >= 1
    # 200 files live under node_modules; a walk that visited them would be
    # well past this. The old rglob implementation scanned every one.
    assert report.entries_scanned < 50, f"walked {report.entries_scanned} entries"
    assert [p.name for p in report.files] == ["README.md"]


@pytest.mark.parametrize(
    ("relative", "patterns", "pruned"),
    [
        ("node_modules", ["**/node_modules/**"], True),
        (".git", ["**/.git/**"], True),
        ("src/.venv", ["**/.venv/**"], True),
        ("src", ["**/node_modules/**"], False),
        ("docs", [], False),
    ],
)
def test_directory_prune_matching(relative: str, patterns: list[str], pruned: bool) -> None:
    assert should_prune_directory(relative, patterns) is pruned


def test_walk_is_deterministic(home: Path) -> None:
    """Idempotency depends on the walk returning a stable order."""

    kwargs = {"include": ["**/*"], "exclude": [], "budget": WalkBudget.unlimited()}
    assert walk_source(home, **kwargs).files == walk_source(home, **kwargs).files


# Creating a symlink on Windows needs Administrator or Developer Mode, so the
# fixture cannot be built. The traversal guard itself is platform-neutral.
@pytest.mark.skipif(os.name == "nt", reason="POSIX-only; see comment above.")
def test_symlinks_are_not_followed_by_default(tmp_path: Path) -> None:
    """A home directory routinely contains links that escape or loop."""

    root = tmp_path / "root"
    (root / "real").mkdir(parents=True)
    (root / "real" / "a.md").write_text("# a", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("# should not be reached", encoding="utf-8")
    os.symlink(outside, root / "link")

    report = walk_source(root, include=["**/*.md"], exclude=[], budget=WalkBudget.unlimited())

    assert [p.name for p in report.files] == ["a.md"]


def test_depth_is_applied_during_traversal(tmp_path: Path) -> None:
    root = tmp_path / "deep"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "top.md").write_text("t", encoding="utf-8")
    (root / "a" / "one.md").write_text("1", encoding="utf-8")
    (root / "a" / "b" / "two.md").write_text("2", encoding="utf-8")
    (root / "a" / "b" / "c" / "three.md").write_text("3", encoding="utf-8")

    def names(depth: int | None) -> list[str]:
        report = walk_source(
            root, include=["**/*.md"], exclude=[], max_depth=depth, budget=WalkBudget.unlimited()
        )
        return sorted(p.name for p in report.files)

    assert names(0) == ["top.md"]
    assert names(1) == ["one.md", "top.md"]
    assert names(2) == ["one.md", "top.md", "two.md"]
    assert names(None) == ["one.md", "three.md", "top.md", "two.md"]


# ---------------------------------------------------------------------------
# Budgets refuse rather than truncate
# ---------------------------------------------------------------------------


def test_budget_stops_the_walk_and_names_the_limit(home: Path) -> None:
    report = walk_source(
        home,
        include=["**/*.json"],
        exclude=[],
        budget=WalkBudget(max_files=5, max_file_size_bytes=None, max_total_bytes=None),
    )

    assert report.limit_hit == "max_files"
    assert report.file_count <= 5


def test_oversized_files_are_skipped_not_fatal(tmp_path: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    (root / "small.md").write_text("ok", encoding="utf-8")
    (root / "huge.md").write_text("x" * 5000, encoding="utf-8")

    report = walk_source(
        root,
        include=["**/*.md"],
        exclude=[],
        budget=WalkBudget(max_files=None, max_file_size_bytes=1000, max_total_bytes=None),
    )

    assert [p.name for p in report.files] == ["small.md"]
    assert report.oversized and report.oversized[0][0] == "huge.md"
    assert report.limit_hit is None


def test_sync_over_budget_indexes_nothing_and_explains(tmp_path: Path, home: Path) -> None:
    """A refused sync must be a clean stop, not a partial index."""

    config = _config(
        tmp_path,
        home,
        sync={"limits": {"max_files": 2, "max_file_size_mb": 25, "max_total_mb": 4096}},
    )
    config.sources = [
        SourceConfig(
            name="home",
            type=SourceType.document_folder,
            path=home,
            include=["**/*.json"],
        )
    ]
    engine = SyncEngine(config)
    try:
        result = engine.sync_source("home", "full")
    finally:
        engine.close()

    assert result.status == "limit_exceeded"
    assert result.indexed_artifacts == 0
    assert "refusing to sync" in result.details["error"]
    # The message has to be actionable, not just "limit exceeded".
    assert "max_depth" in result.details["error"]
    assert result.details["scan"]["limit_hit"] == "max_files"


def test_full_scan_lifts_the_budget_and_the_depth_cap(tmp_path: Path, home: Path) -> None:
    config = _config(
        tmp_path,
        home,
        sync={"limits": {"max_files": 2, "max_file_size_mb": 25, "max_total_mb": 4096}},
    )
    config.sources = [
        SourceConfig(
            name="home",
            type=SourceType.document_folder,
            path=home,
            include=["**/*.json"],
            max_depth=1,
        )
    ]
    engine = SyncEngine(config)
    try:
        # The source's own depth cap keeps this run small; full_scan removes
        # both that cap and the budget, so the second run sees everything.
        capped = engine.sync_source("home", "full")
        allowed = engine.sync_source("home", "full", full_scan=True)
    finally:
        engine.close()

    assert capped.indexed_artifacts <= 2
    assert allowed.status != "limit_exceeded"
    assert allowed.indexed_artifacts > 2

    resolved = config.effective_source(config.sources[0], full_scan=True)
    assert resolved.max_depth is None
    assert resolved.limits.max_files is None
    assert resolved.limits.max_total_mb is None

    # Per-run only: the stored source keeps its own depth and no override.
    assert config.sources[0].max_depth == 1
    assert config.sources[0].limits is None


def test_depth_override_is_per_run_only(tmp_path: Path, home: Path) -> None:
    config = _config(tmp_path, home)
    config.sources = [
        SourceConfig(name="home", type=SourceType.document_folder, path=home, include=["**/*.md"])
    ]
    engine = SyncEngine(config)
    try:
        shallow = engine.sync_source("home", "full", max_depth=0)
        deep = engine.sync_source("home", "full")
    finally:
        engine.close()

    assert shallow.indexed_artifacts == 0  # README.md sits at depth 2
    assert deep.indexed_artifacts >= 1
    assert config.sources[0].max_depth is None


# ---------------------------------------------------------------------------
# scan: know the size before paying for it
# ---------------------------------------------------------------------------


def test_scan_reports_shape_without_indexing(tmp_path: Path, home: Path) -> None:
    config = _config(tmp_path, home)
    config.sources = [
        SourceConfig(name="home", type=SourceType.document_folder, path=home, include=["**/*.md"])
    ]
    engine = SyncEngine(config)
    try:
        report = engine.scan_source("home")
        indexed = engine.state.rows("SELECT COUNT(*) AS n FROM artifacts")[0]["n"]
    finally:
        engine.close()

    assert report["scannable"] is True
    assert report["file_count"] >= 1
    assert report["would_sync"] is True
    assert indexed == 0, "scan must not index anything"


def test_scan_predicts_the_refusal_and_offers_depths(tmp_path: Path, home: Path) -> None:
    config = _config(
        tmp_path,
        home,
        sync={"limits": {"max_files": 2, "max_file_size_mb": 25, "max_total_mb": 4096}},
    )
    config.sources = [
        SourceConfig(name="home", type=SourceType.document_folder, path=home, include=["**/*.json"])
    ]
    engine = SyncEngine(config)
    try:
        report = engine.scan_source("home")
    finally:
        engine.close()

    assert report["would_sync"] is False
    assert any("max_files" in item for item in report["would_exceed"])
    # The depth table is what makes the cap choosable from evidence.
    assert report["depth_options"]
    assert all({"max_depth", "files"} <= set(option) for option in report["depth_options"])


def test_scan_endpoint_and_sync_toggles_over_http(tmp_path: Path, home: Path) -> None:
    config = _config(tmp_path, home)
    client = _client(config, tmp_path)
    client.post(
        "/sources",
        json={
            "name": "home",
            "type": "document_folder",
            "path": str(home),
            "include": ["**/*.md"],
            "sync_now": False,
        },
    )

    scan = client.post("/sources/home/scan")
    assert scan.status_code == 200
    assert scan.json()["file_count"] >= 1

    shallow = client.post("/sync/home", json={"mode": "full", "depth": 0})
    assert shallow.status_code == 200
    assert shallow.json()["indexed_artifacts"] == 0

    full = client.post("/sync/home", json={"mode": "full", "full_scan": True})
    assert full.status_code == 200
    assert full.json()["indexed_artifacts"] >= 1


# ---------------------------------------------------------------------------
# Credentials stay out of the index
# ---------------------------------------------------------------------------


def test_secret_patterns_survive_a_caller_supplied_exclude(tmp_path: Path, home: Path) -> None:
    """Supplying `exclude` replaces the field — it must not drop the secrets."""

    config = _config(tmp_path, home)
    client = _client(config, tmp_path)
    response = client.post(
        "/sources",
        json={
            "name": "home",
            "type": "document_folder",
            "path": str(home),
            "include": ["**/*"],
            "exclude": ["**/build/**"],  # a list that mentions no secret pattern
            "sync_now": True,
        },
    )
    assert response.status_code == 200, response.text

    for marker in SECRET_MARKERS:
        hits = client.post("/search", json={"query": marker, "max_results": 10}).json()["results"]
        assert not any(marker in str(hit) for hit in hits), f"{marker} leaked into the index"

    docs = client.post("/search", json={"query": "ordinary project documentation"}).json()
    assert any("documentation" in str(hit) for hit in docs["results"]), "real content must survive"


def test_effective_source_unions_secret_excludes(tmp_path: Path, home: Path) -> None:
    config = _config(tmp_path, home)
    source = SourceConfig(
        name="home", type=SourceType.document_folder, path=home, exclude=["**/mine/**"]
    )

    resolved = config.effective_source(source)

    assert "**/mine/**" in resolved.exclude
    for pattern in SECRET_EXCLUDES:
        assert pattern in resolved.exclude
    # The caller's own SourceConfig is untouched.
    assert source.exclude == ["**/mine/**"]


def test_source_processing_policy_overrides_every_source_without_mutating_it(
    tmp_path: Path,
) -> None:
    config = PheasantConfig.model_validate(
        {"sync": {"source_processing": {"chunk_max_chars": 2000, "taxonomy_enabled": True}}}
    )
    source = SourceConfig(name="uploaded", type=SourceType.document_folder, path=tmp_path)
    source.chunking.max_chars = 3000

    resolved = config.effective_source(source)

    assert resolved.chunking.max_chars == 2000
    assert resolved.taxonomy.enabled is True
    assert source.chunking.max_chars == 3000
    assert source.taxonomy.enabled is False
    assert PheasantConfig().effective_source(source).chunking.max_chars == 3000
    assert PheasantConfig().effective_source(source).taxonomy.enabled is False


def test_source_processing_policy_rejects_chunk_size_below_overlap(tmp_path: Path) -> None:
    config = PheasantConfig.model_validate(
        {"sync": {"source_processing": {"chunk_max_chars": 200}}}
    )
    source = SourceConfig(name="uploaded", type=SourceType.document_folder, path=tmp_path)

    with pytest.raises(ValueError, match="chunk_max_chars must exceed"):
        config.effective_source(source)


def test_secret_exclusion_can_be_turned_off_explicitly(tmp_path: Path, home: Path) -> None:
    """It is a policy, not a cage — but turning it off has to be deliberate."""

    config = _config(tmp_path, home, security={"default_exclude_secrets": False})
    source = SourceConfig(name="home", type=SourceType.document_folder, path=home, exclude=[])

    resolved = config.effective_source(source)

    assert resolved.exclude == []


# ---------------------------------------------------------------------------
# Bind address
# ---------------------------------------------------------------------------


def test_quickstart_config_binds_loopback(tmp_path: Path) -> None:
    """A laptop install must not answer the whole network unauthenticated."""

    import yaml

    from pheasant.quickstart import render_up_config

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# a", encoding="utf-8")

    rendered = yaml.safe_load(render_up_config(notes, tmp_path / "pheasant.yaml"))

    assert rendered["server"]["host"] == "127.0.0.1"
