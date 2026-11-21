"""Step 33.8 — memory that improves *every* query.

Acceptance: an `alias` record makes a query that previously missed land; a
`preference` re-ranks toward the paths it names; an `exclusion` suppresses;
steering off is byte-identical; a `session`-scoped rule provably cannot affect
another session's query; and a malformed rule is ignored rather than raised.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pheasant.artifacts.policy import MemoryPolicy
from pheasant.artifacts.steering import Steering, load_steering, parse_rule
from pheasant.artifacts.store import MemoryStore
from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools

NOW = "2026-07-16T12:00:00Z"


def _config(tmp_path: Path, *, steering: bool) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    # Deliberately never says "router": the alias test's baseline has to be a
    # genuine miss, or it proves nothing about expansion.
    (docs / "deploy.md").write_text(
        "# Deploying\n\nThe pheasant-flock service fans out to each region.\n",
        encoding="utf-8",
    )
    vendor = tmp_path / "docs" / "vendor"
    vendor.mkdir(exist_ok=True)
    (vendor / "third_party.md").write_text(
        "# Vendor notes\n\nThe pheasant-flock service is described here too.\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: steer-test
  state_path: {tmp_path / ".pheasant" / "state"}
  exports_path: {tmp_path / ".pheasant" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
memory:
  steering_enabled: {"true" if steering else "false"}
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
    (tmp_path / "memory").mkdir(exist_ok=True)
    return config_path


def _synced(tmp_path: Path, rules: list[tuple[str, str, str]], *, steering: bool) -> PheasantTools:
    """`rules` are (kind, scope, text) written straight into the store."""
    config_path = _config(tmp_path, steering=steering)
    store = MemoryStore(tmp_path / "memory")
    for index, (kind, scope, text) in enumerate(rules):
        store.append(
            text, scope=scope, kind=kind, now=datetime(2026, 7, 16, 12, 0, index, tzinfo=UTC)
        )
    tools = PheasantTools(load_config(config_path))
    tools.sync_source("steer-test", "docs", "full")
    tools.sync_source("steer-test", "agent-memory", "full")
    return tools


def _paths(payload: dict) -> list[str]:
    return [str(item.get("relative_path") or "") for item in payload.get("results") or []]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_alias_preference_and_exclusion_parse() -> None:
    assert parse_rule("alias", "router -> pheasant-flock, flock") == (
        "alias",
        ("router", ["pheasant-flock", "flock"]),
    )
    assert parse_rule("preference", "when: deploy, docker -> prefer: docs/, deploy/") == (
        "preference",
        (("deploy", "docker"), ("docs/", "deploy/")),
    )
    assert parse_rule("exclusion", "never: vendor/**") == ("exclusion", ("vendor/**",))


@pytest.mark.parametrize(
    "kind,text",
    [
        ("alias", "no arrow here"),
        ("alias", "-> nothing on the left"),
        ("preference", "when: deploy"),
        ("preference", "prefer: docs/"),
        ("exclusion", "vendor/"),
        ("alias", ""),
    ],
)
def test_a_malformed_rule_is_ignored_not_raised(kind: str, text: str) -> None:
    """These are written by agents into a store read during a sync. One bad
    line must not be able to fail indexing or a search."""
    assert parse_rule(kind, text) is None


def test_a_fact_is_never_a_rule() -> None:
    assert parse_rule("fact", "router -> pheasant-flock") is None


# ---------------------------------------------------------------------------
# Scope gating — a security property
# ---------------------------------------------------------------------------


def _rule(record_id: str, kind: str, scope: str, text: str) -> dict:
    return {
        "record_id": record_id,
        "kind": kind,
        "scope": scope,
        "subject": None,
        "text": text,
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_until": None,
    }


def test_a_session_rule_cannot_steer_another_sessions_query() -> None:
    records = [_rule("mem-1", "alias", "session", "router -> flock")]
    mine = load_steering(records, MemoryPolicy(scopes=("session",)), now=NOW, enabled=True)
    theirs = load_steering(records, MemoryPolicy(scopes=("user",)), now=NOW, enabled=True)
    assert mine.aliases == {"router": ["flock"]}
    assert not theirs


def test_org_steering_is_fleet_wide() -> None:
    records = [_rule("mem-1", "alias", "org", "router -> flock")]
    assert load_steering(records, MemoryPolicy(), now=NOW, enabled=True).aliases


def test_a_corrected_rule_steers_nothing() -> None:
    """The same `admits` predicate decides retrieval and steering, so the two
    can never disagree about what a query is allowed to see."""
    stale = _rule("mem-1", "alias", "org", "router -> flock")
    stale["valid_until"] = "2026-02-01T00:00:00Z"
    assert not load_steering([stale], MemoryPolicy(), now=NOW, enabled=True)


def test_steering_disabled_loads_nothing() -> None:
    records = [_rule("mem-1", "alias", "org", "router -> flock")]
    assert not load_steering(records, MemoryPolicy(), now=NOW, enabled=False)


def test_one_malformed_rule_does_not_take_down_the_others() -> None:
    """`parse_rule` returning None is only half the guarantee — the loader has
    to *skip* it, not raise, or one bad line breaks every search."""
    records = [
        _rule("mem-1", "alias", "org", "this rule has no arrow at all"),
        _rule("mem-2", "alias", "org", "router -> flock"),
        _rule("mem-3", "preference", "org", "when: deploy"),  # missing prefer:
    ]
    rules = load_steering(records, MemoryPolicy(), now=NOW, enabled=True)
    assert rules.aliases == {"router": ["flock"]}
    assert rules.preferences == ()


def test_a_malformed_rule_in_the_store_does_not_break_a_search(tmp_path: Path) -> None:
    tools = _synced(
        tmp_path,
        [("alias", "org", "nonsense with no arrow"), ("alias", "org", "router -> pheasant-flock")],
        steering=True,
    )
    try:
        payload = tools.search_context("steer-test", "router", mode="text", artifact_policy="off")
        assert any("deploy.md" in path for path in _paths(payload))
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# Expansion is additive
# ---------------------------------------------------------------------------


def test_expansion_never_removes_a_callers_own_terms() -> None:
    steering = Steering(aliases={"router": ["pheasant-flock"]})
    expanded = steering.expand(["router", "config"])
    assert set(expanded) >= {"router", "config"}
    assert "pheasant" in expanded and "flock" in expanded


def test_expansion_is_stable() -> None:
    steering = Steering(aliases={"router": ["flock", "pheasant-flock"]})
    assert steering.expand(["router"]) == steering.expand(["router"])


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_an_alias_makes_a_query_that_previously_missed_land(tmp_path: Path) -> None:
    """The headline: steering changes queries that return no memory at all."""
    without = _synced(
        tmp_path / "off", [("alias", "org", "router -> pheasant-flock")], steering=False
    )
    try:
        missed = without.search_context("steer-test", "router", mode="text", artifact_policy="off")
        assert not _paths(missed), "the corpus never says 'router'"
    finally:
        without.engine.close()

    with_steering = _synced(
        tmp_path / "on", [("alias", "org", "router -> pheasant-flock")], steering=True
    )
    try:
        found = with_steering.search_context(
            "steer-test", "router", mode="text", artifact_policy="off"
        )
        assert any("deploy.md" in path for path in _paths(found)), (
            "the alias should have reached the document that only says pheasant-flock"
        )
    finally:
        with_steering.engine.close()


def test_an_exclusion_suppresses_the_paths_it_names(tmp_path: Path) -> None:
    tools = _synced(tmp_path, [("exclusion", "org", "never: vendor/**")], steering=True)
    try:
        paths = _paths(tools.search_context("steer-test", "pheasant-flock", mode="text"))
        assert paths, "expected the non-vendor document"
        assert not any("vendor" in path for path in paths)
    finally:
        tools.engine.close()


def test_a_preference_reranks_toward_the_paths_it_names(tmp_path: Path) -> None:
    tools = _synced(
        tmp_path,
        [("preference", "org", "when: pheasant-flock -> prefer: vendor/")],
        steering=True,
    )
    try:
        paths = _paths(tools.search_context("steer-test", "pheasant-flock", mode="text"))
        assert paths
        assert "vendor" in paths[0], f"preference did not promote vendor/: {paths}"
    finally:
        tools.engine.close()


def test_steering_off_is_byte_identical(tmp_path: Path) -> None:
    """A region that has not asked for steering ranks exactly as it did."""
    rules = [
        ("alias", "org", "router -> pheasant-flock"),
        ("exclusion", "org", "never: vendor/**"),
    ]
    off = _synced(tmp_path / "a", rules, steering=False)
    try:
        baseline = off.search_context(
            "steer-test", "pheasant-flock", mode="text", artifact_policy="off"
        )
    finally:
        off.engine.close()

    plain = _synced(tmp_path / "b", [], steering=False)
    try:
        expected = plain.search_context(
            "steer-test", "pheasant-flock", mode="text", artifact_policy="off"
        )
    finally:
        plain.engine.close()

    assert _paths(baseline) == _paths(expected)
    assert "memory_steering" not in baseline


def test_describe_retrieval_reports_the_rules_in_force(tmp_path: Path) -> None:
    """Steering is invisible otherwise: a query that lands differently because
    someone wrote down a synonym is a mystery unless the region says so."""
    tools = _synced(
        tmp_path,
        [("alias", "org", "router -> pheasant-flock"), ("exclusion", "org", "never: vendor/**")],
        steering=True,
    )
    try:
        described = tools.describe_retrieval("steer-test")["memory"]["steering"]
        assert described["enabled"] is True
        assert described["aliases"] == {"router": ["pheasant-flock"]}
        assert described["exclusions"] == ["vendor/**"]
    finally:
        tools.engine.close()


def test_a_search_reports_the_steering_it_applied(tmp_path: Path) -> None:
    tools = _synced(tmp_path, [("alias", "org", "router -> pheasant-flock")], steering=True)
    try:
        payload = tools.search_context("steer-test", "router", mode="text")
        assert payload["memory_steering"]["aliases"] == {"router": ["pheasant-flock"]}
    finally:
        tools.engine.close()


def test_default_policy_applies_when_the_caller_says_nothing(tmp_path: Path) -> None:
    """`memory.default_policy` fills the gap; a per-call argument still wins."""
    config_path = _config(tmp_path, steering=False)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "memory:\n  steering_enabled: false",
            "memory:\n  steering_enabled: false\n  default_policy: 'off'",
        ),
        encoding="utf-8",
    )
    store = MemoryStore(tmp_path / "memory")
    store.append(
        "A remembered fact about pheasant-flock.",
        scope="org",
        now=datetime(2026, 7, 16, tzinfo=UTC),
    )
    tools = PheasantTools(load_config(config_path))
    try:
        tools.sync_source("steer-test", "docs", "full")
        tools.sync_source("steer-test", "agent-memory", "full")

        silent = tools.search_context("steer-test", "remembered fact", mode="text")
        assert not any(item.get("memory") for item in silent.get("results") or [])

        asked = tools.search_context(
            "steer-test", "remembered fact", mode="text", artifact_policy="only"
        )
        assert any(item.get("memory") for item in asked.get("results") or [])
    finally:
        tools.engine.close()


def test_multi_word_alias_triggers_actually_fire() -> None:
    """A trigger written as several words must match the tokenized query.

    Regression test, found by live evaluation rather than by this suite: every
    alias fixture here happened to be single-word on the left (`router ->
    flock`), and `expand` looked up `aliases[token]` one token at a time. So
    the natural way to write a rule — `filewatch daemon -> fileService,
    watcher` — parsed correctly, loaded correctly, and was reported by
    `describe_retrieval` as being in force, while never once firing. The
    steering feature was inert for exactly the rules people would write.
    """
    steering = Steering(
        aliases={
            "filewatch daemon": ["fileservice", "watcher"],
            "router": ["flock"],
        }
    )

    fired = steering.expand(["filewatch", "daemon"])
    assert "fileservice" in fired and "watcher" in fired
    # Additive: the original tokens always survive.
    assert {"filewatch", "daemon"} <= set(fired)

    # Every part must be present — a partial match must not fire.
    assert "fileservice" not in steering.expand(["daemon", "process"])
    # Single-word triggers keep working exactly as before.
    assert "flock" in steering.expand(["router"])
    # Order stays deterministic: the expansion feeds an FTS MATCH string.
    assert fired == sorted(fired)
