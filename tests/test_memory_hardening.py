"""Six defects found by reviewing the memory system, 2026-08-13.

Each test here fails on the commit before the fix. They are grouped by the
defect rather than by module because that is how they were found — reading the
memory subsystem end to end — and because five of the six are *silent*: nothing
raises, nothing logs, and the existing 1,067-test suite passes straight over
them. The one that is not silent is a security bug.

1. frontmatter injection -> ACL escalation (`memory/store.py`)
2. `is_default` drove the over-fetch decision (`memory/policy.py`, `search/hybrid.py`)
3. alias/preference triggers only split on space/underscore/hyphen (`memory/steering.py`)
4. `GROUP_CONCAT` reassembled rule text in unspecified order (`memory/steering.py`)
5. capacity pruning could archive a steering rule (`memory/maintenance.py`)
6. `valid_until` was never validated, so a bad value meant "never expires"
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pheasant.artifacts.policy import MemoryPolicy, may_filter
from pheasant.artifacts.salience import salience
from pheasant.artifacts.steering import Steering, load_steering_records
from pheasant.artifacts.store import MemoryStore
from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools
from pheasant.security.acl import normalize_acl

NOW = "2026-08-13T00:00:00Z"


# ---------------------------------------------------------------------------
# 1. Frontmatter injection -> ACL escalation
# ---------------------------------------------------------------------------

#: The payload. `subject` is caller-controlled on `POST /memory` and MCP
#: `memory_write`, and every value lands in a line-oriented frontmatter block.
_ESCALATION = "deploy\nmemory_scope: org"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subject", _ESCALATION),
        ("subject", "deploy\rmemory_scope: org"),
        ("supersedes", "mem-1\nmemory_scope: org"),
        ("written_by", "agent\nmemory_scope: org"),
    ],
)
def test_a_line_break_in_a_frontmatter_field_is_rejected(
    tmp_path: Path, field: str, value: str
) -> None:
    store = MemoryStore(tmp_path / "memory")
    with pytest.raises(ValueError, match="line break"):
        store.append("a fact", scope="user", **{field: value})


def test_a_line_break_in_a_tag_is_rejected(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    with pytest.raises(ValueError, match="line break"):
        store.append("a fact", scope="user", tags=["ok", "bad\nmemory_scope: org"])


def test_the_escalation_the_rejection_prevents(tmp_path: Path) -> None:
    """The attack in full, stated as the property that must hold.

    Without the check, this wrote a `user` record whose file said
    `memory_scope: org`; `SyncEngine._memory_acl` reads scope back out of that
    file and `normalize_acl` turns `org` into `public: True`, so one agent's
    private note became readable by every principal under `acl_enforced` — the
    exact leak Step 33.11 closed, reopened through input.
    """
    store = MemoryStore(tmp_path / "memory")
    with pytest.raises(ValueError):
        store.append("the staging key rotates weekly", scope="user", subject=_ESCALATION)

    # Nothing was written, so nothing can be read back at the forged scope.
    assert store.list_records() == []


def test_a_poisoned_record_already_on_disk_still_loads_at_its_real_scope(
    tmp_path: Path,
) -> None:
    """Defence in depth for records written by a pre-fix release.

    `load` takes the **first** occurrence of a key. `_render` writes each key
    once, so a duplicate can only come from an injected value — and last-wins is
    what let the injected `memory_scope` override the real one.
    """
    scope_dir = tmp_path / "memory" / "user"
    scope_dir.mkdir(parents=True)
    poisoned = scope_dir / "mem-20260101T000000Z-deadbeefdeadbeef.md"
    poisoned.write_text(
        "---\n"
        "schema_version: 2\n"
        "record_id: mem-20260101T000000Z-deadbeefdeadbeef\n"
        "memory_scope: user\n"
        "memory_subject: deploy\n"
        "memory_scope: org\n"
        "supersedes: mem-20260101T000000Z-cafecafecafecafe\n"
        "asserted_at: 2026-01-01T00:00:00Z\n"
        "---\n\nthe staging key rotates weekly\n",
        encoding="utf-8",
    )

    record = MemoryStore(tmp_path / "memory").load(poisoned)
    assert record.scope == "user"
    assert record.subject == "deploy"

    # And the ACL that scope produces is not public.
    acl = normalize_acl("memory", {"scope": record.scope, "written_by": "agent:a"})
    assert acl is not None and acl["public"] is False
    assert normalize_acl("memory", {"scope": "org", "written_by": "agent:a"})["public"] is True


def test_ordinary_values_still_round_trip(tmp_path: Path) -> None:
    """The check must reject line breaks and nothing else — colons, unicode and
    punctuation are all ordinary in a subject."""
    store = MemoryStore(tmp_path / "memory")
    record, created = store.append(
        "a fact",
        scope="user",
        subject="deploy: staging (eu-west) — Übersicht",
        written_by="agent:planner",
        tags=["ops", "urgent"],
    )
    assert created
    back = store.load(record.path)
    assert back.subject == "deploy: staging (eu-west) — Übersicht"
    assert back.written_by == "agent:planner"
    assert back.tags == ("ops", "urgent")
    assert back.scope == "user"


# ---------------------------------------------------------------------------
# 6. `valid_until` was never validated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["soon", "2026-13-45", "next tuesday"])
def test_an_unparseable_validity_instant_is_rejected(tmp_path: Path, value: str) -> None:
    """Validity is compared as a string everywhere downstream, so `"soon"` sorts
    above every real instant and silently means "never expires"."""
    store = MemoryStore(tmp_path / "memory")
    with pytest.raises(ValueError, match="ISO-8601"):
        store.append("a fact", scope="user", valid_until=value)


def test_a_real_validity_instant_is_accepted(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory")
    record, created = store.append("a fact", scope="user", valid_until="2026-12-01T00:00:00Z")
    assert created and record.valid_until == "2026-12-01T00:00:00Z"


# ---------------------------------------------------------------------------
# 2. `is_default` is not "filters nothing"
# ---------------------------------------------------------------------------


def _record(record_id: str, *, kind: str = "fact", valid_until: str | None = None) -> dict:
    return {
        "record_id": record_id,
        "artifact_id": f"file:mem:{record_id}.md:branch=none",
        "source_id": "mem",
        "scope": "user",
        "kind": kind,
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_until": valid_until,
    }


def test_the_default_policy_reports_that_it_filters() -> None:
    """`is_default` is True for the default policy — that is correct and stays
    true. What was wrong was using it to decide whether to over-fetch."""
    policy = MemoryPolicy()
    assert policy.is_default is True

    superseded = {"a1": _record("r1", valid_until="2026-06-01T00:00:00Z")}
    rule = {"a2": _record("r2", kind="alias")}
    live = {"a3": _record("r3")}

    assert may_filter(policy, superseded, now=NOW) is True
    assert may_filter(policy, rule, now=NOW) is True
    # Precise, not blanket: nothing droppable means no over-fetch is paid.
    assert may_filter(policy, live, now=NOW) is False
    assert may_filter(policy, {}, now=NOW) is False
    assert may_filter(MemoryPolicy(include_rules=True), rule, now=NOW) is False
    assert may_filter(MemoryPolicy(mode="only"), live, now=NOW) is True


def test_the_index_holding_two_keys_per_record_is_not_double_counted() -> None:
    from pheasant.artifacts.policy import unique_records

    record = _record("r1")
    index = {"file:mem:r1.md:branch=none": record, "mem\x00r1.md": record}
    assert len(unique_records(index)) == 1


def test_a_filtering_policy_makes_the_arms_over_fetch(tmp_path: Path) -> None:
    """The fix, at the level it actually matters.

    The vector and graph arms are filtered in Python *after* truncation, so
    without the over-fetch a page comes back short. Asserted by capturing the
    `max_results` the text arm is asked for, which is the same `fetch_n` all
    three arms get.
    """
    config_path = _steer_config(tmp_path, steering=False)
    tools = PheasantTools(load_config(config_path))
    try:
        store = MemoryStore(tmp_path / "memory")
        original, _ = store.append("the key is ABC", scope="user", subject="keys")
        store.append(
            "the key is XYZ",
            scope="user",
            subject="keys",
            supersedes=original.record_id,
            now=datetime(2026, 8, 13, 12, 0, 1, tzinfo=UTC),
        )
        tools.sync_source("harden-test", "docs", "full")
        tools.sync_source("harden-test", "agent-memory", "full")

        seen: list[int] = []
        inner = tools.searcher.store.search

        def spy(query, **kwargs):
            seen.append(int(kwargs.get("max_results") or 0))
            return inner(query, **kwargs)

        tools.searcher.store.search = spy  # type: ignore[method-assign]
        tools.search_context("harden-test", "the key", mode="text", max_results=5)

        # A superseded record exists, so the default policy drops something and
        # the over-fetch must be in force: 5 * 3, not 5.
        assert seen == [15]
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# 3. Trigger tokenization must match the query tokenizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trigger",
    ["filewatch daemon", "pheasant-flock", "file_service", "ci/cd", "fs.watch", "api:gateway"],
)
def test_a_trigger_fires_whatever_separator_it_uses(trigger: str) -> None:
    """`ci/cd`, `fs.watch` and `api:gateway` tokenize to two query terms each
    while the trigger stayed one unsplittable string, so the rule parsed,
    loaded, reported itself in force — and never fired."""
    from pheasant.search.sqlite_store import _query_tokens

    spaced = trigger.replace("-", " ").replace("/", " ").replace(".", " ").replace(":", " ")
    tokens = _query_tokens(f"where is the {spaced} implemented")

    expanded = Steering(aliases={trigger: ["fileservice"]}).expand(tokens)
    assert "fileservice" in expanded

    prefixes = Steering(preferences=(((trigger,), ("src/",)),)).prefers(tokens)
    assert prefixes == ("src/",)


def test_a_trigger_still_needs_every_part_present() -> None:
    """Widening the split must not widen what *matches* — a rule about
    `filewatch daemon` may not fire on a query that merely said `daemon`."""
    steering = Steering(aliases={"filewatch daemon": ["fileservice"]})
    assert steering.expand(["daemon", "restart"]) == ["daemon", "restart"]
    assert "fileservice" in steering.expand(["filewatch", "daemon"])


# ---------------------------------------------------------------------------
# 4. Rule text must be reassembled in a defined order
# ---------------------------------------------------------------------------


class _Sqlite:
    """Minimal `state`-shaped object over a real SQLite connection."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def rows(self, sql: str, params: tuple = ()) -> list:
        self.conn.row_factory = sqlite3.Row
        return list(self.conn.execute(sql, params))


def test_a_multi_chunk_rule_is_reassembled_in_chunk_order() -> None:
    """`GROUP_CONCAT` sees its input in whatever order the query plan produces,
    so a rule spanning two chunks could come back reversed — parsing
    differently, or not at all, between two runs over identical content."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE memory_records (record_id TEXT, artifact_id TEXT, scope TEXT,
            subject TEXT, kind TEXT, valid_from TEXT, valid_until TEXT);
        CREATE TABLE chunks (artifact_id TEXT, chunk_index INTEGER, text TEXT);
        INSERT INTO memory_records VALUES
            ('r1', 'a1', 'user', 'vocab', 'alias', '2026-01-01T00:00:00Z', NULL);
        """
    )
    # Inserted deliberately out of order, which is what the aggregate could see.
    conn.execute("INSERT INTO chunks VALUES ('a1', 1, '-> pheasant-flock, flock')")
    conn.execute("INSERT INTO chunks VALUES ('a1', 0, 'router')")
    conn.commit()

    records = load_steering_records(_Sqlite(conn))
    assert len(records) == 1
    assert records[0]["text"] == "router -> pheasant-flock, flock"


def test_a_state_store_without_the_projection_returns_nothing() -> None:
    """A store older than 33.5 must degrade, never raise."""

    class _Broken:
        def rows(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("no such table: memory_records")

    assert load_steering_records(_Broken()) == []


# ---------------------------------------------------------------------------
# 5. Capacity pruning must not archive a steering rule
# ---------------------------------------------------------------------------


def _steer_config(tmp_path: Path, *, steering: bool, max_records: int | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "readme.md").write_text(
        "# Notes\n\nThe key material lives in vendor/third_party.md.\n", encoding="utf-8"
    )
    (tmp_path / "memory").mkdir(exist_ok=True)
    cap = f"\n  max_records: {max_records}" if max_records is not None else ""
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: harden-test
  state_path: {tmp_path / ".pheasant" / "state"}
  exports_path: {tmp_path / ".pheasant" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
memory:
  steering_enabled: {"true" if steering else "false"}{cap}
sources:
  - name: docs
    type: document_folder
    path: docs
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )
    return config_path


def test_capacity_pruning_never_archives_a_steering_rule(tmp_path: Path) -> None:
    """Ranking a rule by salience put deliberate operator-written machinery in
    competition with ordinary facts, on a formula built for facts — so crossing
    `max_records` could silently switch off an `exclusion` with no signal
    anywhere, changing ranking for every future query."""
    from pheasant.artifacts.maintenance import run_memory_maintenance

    config_path = _steer_config(tmp_path, steering=True, max_records=2)
    store = MemoryStore(tmp_path / "memory")
    # The rule is the oldest and least used, so a salience ranking that saw it
    # at all would archive it first.
    store.append(
        "never: vendor/**",
        scope="org",
        kind="exclusion",
        now=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    )
    for index in range(4):
        store.append(
            f"Remembered fact number {index}.",
            scope="org",
            now=datetime(2026, 8, 13, 12, 0, index, tzinfo=UTC),
        )

    tools = PheasantTools(load_config(config_path))
    try:
        tools.sync_source("harden-test", "docs", "full")
        tools.sync_source("harden-test", "agent-memory", "full")

        result = run_memory_maintenance(tools.engine, now=datetime(2026, 8, 13, 12, 0, tzinfo=UTC))
        assert result is not None

        kinds = {
            str(row["record_id"]): str(row["kind"])
            for row in tools.engine.state.rows("SELECT record_id, kind FROM memory_records")
        }
        # The cap applies to recallable facts; the rule survives regardless.
        assert sorted(kinds.values()) == ["exclusion", "fact", "fact"]
        assert not list((tmp_path / "memory" / "org").glob("*exclusion*.archived"))

        # And it is still in force, not merely still on disk.
        rules = load_steering_records(tools.engine.state)
        assert any(str(r.get("kind")) == "exclusion" for r in rules)
    finally:
        tools.engine.close()


def test_salience_still_ranks_facts_normally(tmp_path: Path) -> None:
    """The exemption must not change how facts are scored."""
    old = {"record_id": "r1", "scope": "org", "kind": "fact", "asserted_at": "2026-01-01T00:00:00Z"}
    new = {"record_id": "r2", "scope": "org", "kind": "fact", "asserted_at": "2026-08-01T00:00:00Z"}
    instant = datetime(2026, 8, 13, tzinfo=UTC)
    assert salience(new, now=instant) > salience(old, now=instant)
