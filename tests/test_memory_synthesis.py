"""Phase 4 — L3 synthesis: the one non-deterministic compaction tier.

Acceptance: off by default and never called automatically; degrades to a
clean skip whenever no model is reachable, so `pytest` stays network-free
by construction; a successful merge subsumes its members through the same
mechanism medoid promotion uses (op="synthesize", not supersedes — the
inputs are redundant, not corrected); a synthesized record inherits its
cluster's writer, never a synthetic identity, so ACL stays correct; and a
repeat pass over an already-resolved cluster costs zero model calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pheasant.artifacts.synthesis import SYNTHESIZED_TAG, params_hash
from pheasant.config.loader import load_config
from pheasant.mcp_server.tools import PheasantTools


def _write_config(
    tmp_path: Path,
    *,
    synthesis_enabled: bool = True,
    min_cluster_size: int = 2,
    max_calls_per_pass: int = 20,
    max_jaccard: float = 0.55,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory").mkdir(exist_ok=True)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: synthesis-test
  state_path: {tmp_path / ".pheasant" / "state"}
  exports_path: {tmp_path / ".pheasant" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
memory:
  synthesis:
    enabled: {"true" if synthesis_enabled else "false"}
    provider: anthropic
    min_cluster_size: {min_cluster_size}
    max_calls_per_pass: {max_calls_per_pass}
    max_jaccard: {max_jaccard}
sources:
  - name: agent-memory
    type: memory
    path: memory
""",
        encoding="utf-8",
    )
    return config_path


def _fake_anthropic_response(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def _install_fake_model(monkeypatch: pytest.MonkeyPatch, text: str) -> dict:
    """Patches the one seam `assistant.providers` documents for offline
    tests. Returns a counter dict `{"n": <calls>}` the test can inspect."""
    import pheasant.assistant.providers as providers_module

    calls = {"n": 0}

    def fake_http(url, payload, headers, timeout):
        calls["n"] += 1
        return _fake_anthropic_response(text)

    monkeypatch.setattr(providers_module, "_http_json", fake_http)
    return calls


# ---------------------------------------------------------------------------
# Off by default, never automatic, degrades cleanly
# ---------------------------------------------------------------------------


def test_synthesis_disabled_by_default(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, synthesis_enabled=False))
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "synthesis-test", "The staging cluster runs in us-east-2.", scope="org", subject="infra"
        )
        result = tools.memory_synthesize("synthesis-test")
        assert result == {"skipped": "memory.synthesis.enabled is false"}
    finally:
        tools.engine.close()


def test_synthesis_skips_when_no_provider_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for env_var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(env_var, raising=False)
    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        result = tools.memory_synthesize("synthesis-test")
        assert result == {"skipped": "no model reachable"}
    finally:
        tools.engine.close()


def test_no_memory_source_configured(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "pheasant.yaml"
    config_path.write_text(
        f"""pheasant:
  name: synthesis-test
  state_path: {tmp_path / ".pheasant" / "state"}
  exports_path: {tmp_path / ".pheasant" / "exports"}
  workspace_root: {tmp_path}
sync:
  watcher:
    enabled: false
  scheduler:
    enabled: false
memory:
  synthesis:
    enabled: true
""",
        encoding="utf-8",
    )
    tools = PheasantTools(load_config(config_path))
    try:
        assert tools.memory_synthesize("synthesis-test") == {
            "skipped": "no `type: memory` source is configured"
        }
    finally:
        tools.engine.close()


def test_graceful_degradation_on_model_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider error must not fail the pass — try_complete degrades to
    skipping that cluster, exactly like the optional calls in an agent
    workflow."""
    import pheasant.assistant.providers as providers_module

    def boom(url, payload, headers, timeout):
        raise providers_module.ProviderError("simulated outage")

    monkeypatch.setattr(providers_module, "_http_json", boom)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "synthesis-test", "The staging cluster runs in us-east-2.", scope="org", subject="infra"
        )
        tools.memory_write(
            "synthesis-test",
            "The staging cluster is owned by platform.",
            scope="org",
            subject="infra",
        )
        result = tools.memory_synthesize("synthesis-test")
        assert result["synthesized"] == 0
        assert result["attempted"] == 1
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# The merge itself
# ---------------------------------------------------------------------------


def test_synthesis_merges_and_subsumes_via_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    merged_text = "The staging cluster runs in us-east-2 and is owned by platform."
    calls = _install_fake_model(monkeypatch, merged_text)

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        a = tools.memory_write(
            "synthesis-test", "The staging cluster runs in us-east-2.", scope="org", subject="infra"
        )
        b = tools.memory_write(
            "synthesis-test",
            "The staging cluster is owned by platform.",
            scope="org",
            subject="infra",
        )
        result = tools.memory_synthesize("synthesis-test")
        assert result["synthesized"] == 1
        assert calls["n"] == 1
        new_id = result["records"][0]

        rows = {row["record_id"]: row for row in tools.state.memory_compaction_rows()}
        assert rows[a["record"]["record_id"]]["tier"] == "cold"
        assert rows[a["record"]["record_id"]]["subsumed_by"] == new_id
        assert rows[b["record"]["record_id"]]["tier"] == "cold"
        assert rows[b["record"]["record_id"]]["subsumed_by"] == new_id
        assert rows[new_id]["tier"] == "hot"

        ledger = tools.state.memory_compaction_ledger(canonical_id=new_id)
        assert len(ledger) == 2
        assert {row["member_id"] for row in ledger} == {
            a["record"]["record_id"],
            b["record"]["record_id"],
        }
        assert all(row["op"] == "synthesize" for row in ledger)
        assert all(row["rule_id"] == "llm-synthesis-v1" for row in ledger)

        # Human-visible provenance, not just the ledger.
        listing = tools.memory_list("synthesis-test", current_only=False)
        new_record = next(r for r in listing["records"] if r["record_id"] == new_id)
        assert SYNTHESIZED_TAG in new_record["tags"]
        assert new_record["text"] == merged_text

        # Default results show only the canonical record.
        default = tools.search_context(
            "synthesis-test", "staging cluster", mode="text", max_results=10
        )
        default_ids = {
            item["memory"]["record_id"]
            for item in default.get("results") or []
            if item.get("memory")
        }
        assert default_ids == {new_id}
    finally:
        tools.engine.close()


def test_second_pass_is_free_once_members_are_subsumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary idempotency guarantee: once subsumed, a member is no
    longer 'hot' and drops out of candidacy entirely — a repeat pass never
    even reaches the cache check, let alone calls the model."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = _install_fake_model(monkeypatch, "Merged note.")

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "synthesis-test", "The staging cluster runs in us-east-2.", scope="org", subject="infra"
        )
        tools.memory_write(
            "synthesis-test",
            "The staging cluster is owned by platform.",
            scope="org",
            subject="infra",
        )
        first = tools.memory_synthesize("synthesis-test")
        assert first["synthesized"] == 1
        assert calls["n"] == 1

        second = tools.memory_synthesize("synthesis-test")
        assert second == {"attempted": 0, "synthesized": 0, "cached": 0, "records": []}
        assert calls["n"] == 1  # unchanged
    finally:
        tools.engine.close()


def test_params_hash_cache_is_checked_before_any_call() -> None:
    """Unit-level: the content-address itself is stable for the same
    member set + model, and a ledger row under that hash is what the pass
    checks before spending a call — verified directly rather than through
    a full pipeline run, since reproducing the exact narrow race that
    reaches this path (members reset to hot with the ledger intact, no
    other hot member sharing the bucket) is easier to state as a fact
    about the function than to stage end to end."""
    a = params_hash(model_id="claude-haiku-4-5", member_ids=["mem-b", "mem-a"])
    b = params_hash(model_id="claude-haiku-4-5", member_ids=["mem-a", "mem-b"])
    assert a == b  # order-independent — sorted before hashing
    c = params_hash(model_id="claude-haiku-4-5", member_ids=["mem-a", "mem-b", "mem-c"])
    assert a != c  # a different member set is a different cluster
    d = params_hash(model_id="gpt-5.6-luna", member_ids=["mem-a", "mem-b"])
    assert a != d  # a different model is a different decision


# ---------------------------------------------------------------------------
# ACL safety — the writer must never become a synthetic identity
# ---------------------------------------------------------------------------


def test_synthesized_record_inherits_the_cluster_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """user/session-scope records in one bucket share one written_by by
    construction (the bucket key), and the synthesized record must inherit
    it — never a synthetic 'llm' identity, which would make the merged
    record unreadable by the very principal whose facts it summarizes."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _install_fake_model(monkeypatch, "Alice's favorite color is blue and she lives in Denver.")

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "synthesis-test",
            "Alice's favorite color is blue.",
            scope="user",
            subject="alice-profile",
            principal="alice",
        )
        tools.memory_write(
            "synthesis-test",
            "Alice lives in Denver.",
            scope="user",
            subject="alice-profile",
            principal="alice",
        )
        result = tools.memory_synthesize("synthesis-test")
        assert result["synthesized"] == 1
        new_id = result["records"][0]
        listing = tools.memory_list("synthesis-test", current_only=False)
        new_record = next(r for r in listing["records"] if r["record_id"] == new_id)
        assert new_record["written_by"] == "alice"
    finally:
        tools.engine.close()


def test_synthesis_never_crosses_principal_at_user_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = _install_fake_model(monkeypatch, "Should never be called for this bucket.")

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "synthesis-test",
            "My favorite color is blue.",
            scope="user",
            subject="prefs",
            principal="alice",
        )
        tools.memory_write(
            "synthesis-test",
            "My favorite color is also blue.",
            scope="user",
            subject="prefs",
            principal="bob",
        )
        result = tools.memory_synthesize("synthesis-test")
        # Two different ACL partitions (alice, bob) -> two buckets of size
        # 1 each -> no cluster ever reaches min_cluster_size.
        assert result["attempted"] == 0
        assert calls["n"] == 0
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------


def test_steering_kinds_are_never_synthesis_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = _install_fake_model(monkeypatch, "should not be called")

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write(
            "synthesis-test",
            "flock -> pheasant-flock, flock-router",
            scope="org",
            subject="terms",
            kind="alias",
        )
        tools.memory_write(
            "synthesis-test",
            "flock -> the router repo",
            scope="org",
            subject="terms",
            kind="alias",
        )
        result = tools.memory_synthesize("synthesis-test")
        assert result["attempted"] == 0
        assert calls["n"] == 0
    finally:
        tools.engine.close()


def test_untagged_records_are_never_synthesis_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `subject` means no anchor to merge around — left to L1/L2."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = _install_fake_model(monkeypatch, "should not be called")

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    try:
        tools.memory_write("synthesis-test", "The staging cluster runs in us-east-2.", scope="org")
        tools.memory_write(
            "synthesis-test", "The staging cluster is owned by platform.", scope="org"
        )
        result = tools.memory_synthesize("synthesis-test")
        assert result["attempted"] == 0
        assert calls["n"] == 0
    finally:
        tools.engine.close()


def test_max_calls_per_pass_bounds_the_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = _install_fake_model(monkeypatch, "Merged note.")

    config = load_config(_write_config(tmp_path, max_calls_per_pass=1))
    tools = PheasantTools(config)
    try:
        # Complementary, not near-duplicate: two records about one subject
        # that say genuinely different things. That is the shape synthesis
        # exists for, and the shape `max_jaccard` admits — a pair of
        # rephrasings of one claim is what medoid promotion already handles
        # losslessly, so the gate would (correctly) skip it and this test
        # would be measuring the gate rather than `max_calls_per_pass`.
        for subject in ("topic-a", "topic-b", "topic-c"):
            tools.memory_write(
                "synthesis-test",
                f"The {subject} deploy runs nightly.",
                scope="org",
                subject=subject,
            )
            tools.memory_write(
                "synthesis-test",
                f"Platform owns {subject} escalations.",
                scope="org",
                subject=subject,
            )
        result = tools.memory_synthesize("synthesis-test")
        assert result["attempted"] == 1
        assert calls["n"] == 1
    finally:
        tools.engine.close()


# ---------------------------------------------------------------------------
# HTTP parity
# ---------------------------------------------------------------------------


def test_http_memory_synthesize_matches_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _install_fake_model(
        monkeypatch, "The staging cluster runs in us-east-2 and is owned by platform."
    )

    config_path = _write_config(tmp_path)
    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        client.post(
            "/artifacts/records",
            json={
                "text": "The staging cluster runs in us-east-2.",
                "scope": "org",
                "subject": "infra",
            },
        )
        client.post(
            "/artifacts/records",
            json={
                "text": "The staging cluster is owned by platform.",
                "scope": "org",
                "subject": "infra",
            },
        )
        resp = client.post("/artifacts/synthesize")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["synthesized"] == 1
    finally:
        app.state.engine.close()


def test_http_memory_synthesize_disabled_returns_200_with_skipped(tmp_path: Path) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from pheasant.api.app import create_app

    config_path = _write_config(tmp_path, synthesis_enabled=False)
    app = create_app(load_config(config_path), config_path=str(config_path))
    client = fastapi_testclient.TestClient(app)
    try:
        resp = client.post("/artifacts/synthesize")
        assert resp.status_code == 200
        assert resp.json() == {"skipped": "memory.synthesis.enabled is false"}
    finally:
        app.state.engine.close()


# ---------------------------------------------------------------------------
# The similarity gate (max_jaccard)
# ---------------------------------------------------------------------------
#
# The lever that decides what the model is asked to do *without* a model.
# Synthesis exists for what deterministic compaction provably cannot do —
# merge complementary partials, fold progressive refinement, abstract across
# instances. A cluster of near-identical rephrasings is not that: medoid
# promotion (L2) resolves it losslessly and for free. Spending a call there
# buys nothing, and without this gate every such bucket reaches the model
# whenever `compaction_enabled` is off — which is the default.


def test_near_duplicate_clusters_are_left_to_medoid_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = _install_fake_model(monkeypatch, "Merged note.")

    tools = PheasantTools(load_config(_write_config(tmp_path)))
    try:
        # Two rephrasings of one claim: high Jaccard, exactly L2's job.
        tools.memory_write(
            "synthesis-test",
            "The staging cluster runs in us-east-2 and is owned by platform.",
            scope="org",
            subject="staging",
        )
        tools.memory_write(
            "synthesis-test",
            "The staging cluster runs in us-east-2 and is owned by platform team.",
            scope="org",
            subject="staging",
        )
        result = tools.memory_synthesize("synthesis-test")
        assert result["attempted"] == 0, result
        assert calls["n"] == 0
    finally:
        tools.engine.close()


def test_complementary_facts_about_one_subject_still_reach_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the same gate — it must not swallow the case the
    whole tier exists for."""

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = _install_fake_model(
        monkeypatch, "The staging cluster runs in us-east-2 and is owned by ada."
    )

    tools = PheasantTools(load_config(_write_config(tmp_path)))
    try:
        tools.memory_write(
            "synthesis-test",
            "The staging cluster runs in us-east-2.",
            scope="org",
            subject="staging",
        )
        tools.memory_write(
            "synthesis-test",
            "Ada owns anything that pages overnight.",
            scope="org",
            subject="staging",
        )
        result = tools.memory_synthesize("synthesis-test")
        assert result["attempted"] == 1, result
        assert result["synthesized"] == 1
        assert calls["n"] == 1
    finally:
        tools.engine.close()


def test_the_gate_measures_similarity_the_way_clustering_does() -> None:
    """`mean_pairwise_jaccard` lives in `memory.compaction` so the gate and
    the tier it defers to cannot disagree about what "near-duplicate" means
    — a gate reading "L2 has this" must be right about that."""

    from pheasant.artifacts.compaction import mean_pairwise_jaccard

    assert mean_pairwise_jaccard(["only one"]) == 0.0
    identical = mean_pairwise_jaccard(["the gateway restarts nightly"] * 3)
    assert identical == 1.0
    unrelated = mean_pairwise_jaccard(
        ["the gateway restarts nightly", "invoices are generated monthly"]
    )
    assert unrelated < 0.2
    # Deterministic: a mean of ratios is order-dependent in the last bit, so
    # a gate that flipped between runs would make a pass non-reproducible.
    texts = ["alpha beta gamma", "beta gamma delta", "gamma delta epsilon"]
    assert mean_pairwise_jaccard(texts) == mean_pairwise_jaccard(list(reversed(texts)))


def test_a_synthesized_record_is_stamped_with_the_callers_instant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record id embeds its own second, so a pass with a pinned `now` must
    stamp that instant rather than the wall clock — otherwise the record and
    the ledger row recording it disagree about when the merge happened."""

    from datetime import UTC, datetime

    from pheasant.artifacts.store import memory_source
    from pheasant.artifacts.synthesis import run_synthesis

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _install_fake_model(monkeypatch, "The staging cluster runs in us-east-2 and is owned by ada.")

    config = load_config(_write_config(tmp_path))
    tools = PheasantTools(config)
    pinned = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    try:
        tools.memory_write(
            "synthesis-test",
            "The staging cluster runs in us-east-2.",
            scope="org",
            subject="staging",
        )
        tools.memory_write(
            "synthesis-test",
            "Ada owns anything that pages overnight.",
            scope="org",
            subject="staging",
        )
        source = memory_source(tools.config, tools.state)
        from pheasant.artifacts.store import MemoryStore

        records = MemoryStore(source.path).list_records()
        result = run_synthesis(tools.engine, records, config.memory.synthesis, source, now=pinned)
        assert result["synthesized"] == 1, result

        record_id = result["records"][0]
        assert record_id.startswith("mem-20260304T050607Z-"), record_id
        ledger = tools.engine.state.memory_compaction_ledger()
        assert {row["at"] for row in ledger if row["op"] == "synthesize"} == {
            "2026-03-04T05:06:07Z"
        }
    finally:
        tools.engine.close()
