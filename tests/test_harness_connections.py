"""Runs that work through connected accounts: results, grounding and pinning.

Every service is a network-free fake that checks the credential the way the
real one does, so a run that succeeds here read exactly what an authorized
account would have read, and the citations it carries resolve against the
revision it pinned -- a commit, a per-file checksum, a document's edit time.
"""
# ruff: noqa: E501, F811

from __future__ import annotations

import re

import pytest

from tests.harness_connection_support import (  # noqa: F401
    REFUND_DOC,
    answer_request,
    leaks,
    world,
)
from tests.harness_support import BADGE_EDIT, PYTEST, events

pytest.importorskip("pheasant_harness")

from pheasant_harness import investigate  # noqa: E402
from pheasant_harness.registry import RevisionMoved  # noqa: E402

EVIDENCE = re.compile(r"\[(E[0-9a-f]{8})\]")


def _cited(world, run_id: str, digest: str) -> list[dict]:
    detail = world.region.detail(run_id)
    by_id = {e["id"]: e for e in detail["evidence"]}
    return [by_id[i] for i in EVIDENCE.findall(world.text(digest)) if i in by_id]


def test_an_answer_through_github_cites_the_commit_it_pinned(world):
    reg = world.handbook()
    final = world.region.run(**answer_request([reg]))
    assert final["state"] == "succeeded", final["result"]
    answer = world.text(final["result"]["candidates"]["primary"])
    assert "discount above 15% need an exception badge" in answer
    head = world.github.head("acme/handbook", "main").sha
    cited = _cited(world, final["run_id"], final["result"]["candidates"]["primary"])
    reads = [e for e in cited if e["method"] in ("read", "grep")]
    assert reads and all(e["revision"] == head for e in reads)
    assert {e["locator"]["path"] for e in reads} == {"docs/discounts.md"}
    checks = {
        c["criterion_id"]: c["outcome"]
        for c in world.region.detail(final["run_id"])["verifications"]
    }
    assert checks["citations-resolve"] == "pass" and checks["claims-grounded"] == "pass"
    # Only GETs reached GitHub, each with the connection's own credential; nothing else was contacted.
    github = world.net.to("api.github.com")
    assert github and {c.method for c in github} == {"GET"}
    assert all(c.authorization == "Bearer " + world.github.token for c in github)
    assert not world.net.unowned
    calls = world.calls(final["run_id"])
    assert {c["action"] for c in calls} == {"list", "read"}
    assert {c["outcome"] for c in calls} <= {"allowed", "cached"}
    # The verifier re-read what was cited, through the same policy, as the verifier.
    assert any(c["purpose"] == "verify" for c in calls)


def test_a_branch_that_moves_after_resolve_does_not_change_what_was_cited(world):
    reg = world.handbook()
    registry = world.harness.registry
    first = registry.resolve(reg, "local")
    world.github.commit(
        "acme/handbook",
        "main",
        {
            "docs/discounts.md": "# Discounts\n\nOrders with a discount above 20% need an exception badge.\n"
        },
        "raise the threshold",
    )
    second = registry.resolve(reg, "local")
    assert first.revision != second.revision and not first.time_bound
    resolver = registry.resolver(registry.get(reg))
    old = investigate.read_range(
        resolver, registry.revision(reg, first.revision, "local"), "docs/discounts.md"
    )
    new = investigate.read_range(resolver, second, "docs/discounts.md")
    assert "15%" in old["content"] and "20%" in new["content"]
    assert investigate.compare(resolver, first, second)["changed"] == ["docs/discounts.md"]
    later = world.region.run(key="k2", **answer_request([reg]))
    assert later["state"] == "succeeded", later["result"]
    assert "20%" in world.text(later["result"]["candidates"]["primary"])


def test_a_drive_folder_is_read_with_its_google_docs_exported_and_pinned(world):
    world.allow("drive", ["list", "read"], [f"{world.policies_folder}/*"])
    reg = world.register(f"connection://drive/{world.policies_folder}")
    final = world.region.run(
        **answer_request(
            [reg], question="When are refunds approved automatically?", identifiers=["30 days"]
        )
    )
    assert final["state"] == "succeeded", final["result"]
    answer = world.text(final["result"]["candidates"]["primary"])
    assert "Refunds within 30 days of delivery are approved automatically" in answer
    cited = _cited(world, final["run_id"], final["result"]["candidates"]["primary"])
    assert {e["locator"]["path"] for e in cited if e["method"] in ("read", "grep")} == {
        "Refund policy.txt"
    }
    revision = world.harness.registry.resolve(reg, "local")
    assert revision.time_bound  # Drive keeps no folder revisions: each read is checked instead
    entry = revision.entry("Refund policy.txt")
    assert entry["export"] == "text/plain" and entry["digest"].startswith("drive-modified:")
    # An edit in Drive after the pin is a conflict, never new text under an old citation.
    world.drive.edit(world.refund_doc, REFUND_DOC.replace("30", "14"), "2026-11-26T12:00:00.000Z")
    registry = world.harness.registry
    resolver = registry.resolver(registry.get(reg))
    with pytest.raises(RevisionMoved, match="edited in Drive"):
        resolver.read_bytes(revision, "Refund policy.txt")
    page = investigate.grep(resolver, revision, "refund", {"case_insensitive": True})
    assert page["coverage"]["changed"] == ["Refund policy.txt"]


def test_a_drive_file_whose_bytes_do_not_match_its_checksum_is_not_read(world):
    world.allow("drive", ["list", "read"], [f"{world.policies_folder}/*"])
    reg = world.register(f"connection://drive/{world.policies_folder}")
    revision = world.harness.registry.resolve(reg, "local")
    notes = revision.entry("notes.md")
    world.drive.serve_stale_bytes.add(notes["id"])
    resolver = world.harness.registry.resolver(world.harness.registry.get(reg))
    with pytest.raises(RevisionMoved):
        resolver.read_bytes(revision, "notes.md")


def test_a_onedrive_library_is_read_and_its_download_never_carries_the_token(world):
    world.allow("m365", ["list", "read"], ["b!drive1:*"])
    reg = world.register("connection://m365/b!drive1/Ops")
    final = world.region.run(
        **answer_request([reg], question="How is the api restarted?", identifiers=["restart-api"])
    )
    assert final["state"] == "succeeded", final["result"]
    assert "make restart-api" in world.text(final["result"]["candidates"]["primary"])
    downloads = world.net.to("contoso.sharepoint.com")
    assert downloads and all(c.authorization is None for c in downloads)
    assert all(
        c.authorization == "Bearer " + world.graph.token
        for c in world.net.to("graph.microsoft.com")
    )


def test_a_listing_the_provider_cut_short_never_supports_an_absence(world):
    reg = world.handbook()
    world.github.truncate_trees = True
    revision = world.harness.registry.resolve(reg, "local")
    gap = [e for e in revision.entries if e.get("listing_gap")]
    assert gap and "truncated" in gap[0]["reason"]
    resolver = world.harness.registry.resolver(world.harness.registry.get(reg))
    page = investigate.grep(resolver, revision, "NO_SUCH_SYMBOL")
    coverage = page["coverage"]
    assert coverage["complete"] is False and page["outcome"] == "partial"
    assert "covered only what the provider listed" in coverage["statement"]
    assert "No occurrences" not in coverage["statement"]


def test_a_grep_reports_files_the_policy_kept_it_from_reading(world):
    reg = world.handbook(resources=["acme/handbook:", "acme/handbook:docs/*"])
    revision = world.harness.registry.resolve(reg, "local")
    resolver = world.harness.registry.resolver(world.harness.registry.get(reg))
    page = investigate.grep(resolver, revision, "charge", {"hidden": True})
    denied = {
        u["path"]
        for u in page["coverage"]["unreadable"]
        if u["reason"].startswith("denied by policy")
    }
    assert denied == {"README.md", "src/billing.py", ".github/workflows/ci.yml"}
    assert page["matches"] == []
    assert "3 unreadable" in page["coverage"]["statement"]
    # Nothing outside docs/ was requested at all.
    blob_reads = [c for c in world.net.to("api.github.com") if "/git/blobs/" in c.path]
    assert len(blob_reads) == 2


def test_an_answer_is_delivered_into_github_once_after_an_operator_approves(world):
    reg = world.handbook(
        actions=["list", "read", "write"],
        write_resources=["acme/handbook@harness/*:answers/*"],
    )
    target = "connection://gh/acme/handbook/answers/badges.md?branch=harness/answers"
    world.harness.registry.grant(reg, "alice", "ops")
    run_id = world.region.start(
        actor="alice",
        **answer_request([reg], destination={"adapter": "connection", "target": target}),
    )["run_id"]
    waiting = world.region.coordinator().drive(run_id)
    assert waiting["state"] == "waiting" and waiting["wait"]["kind"] == "approval"
    assert waiting["wait"]["resource"] == "acme/handbook@harness/answers:answers/badges.md"
    assert "exception badge" in waiting["wait"]["preview"]
    assert not world.net.writes()  # nothing was written before the decision
    coordinator = world.region.coordinator("ops-console")
    coordinator.decide_approval(
        run_id, waiting["wait"]["operation_id"], "approve", "ops", operator=True
    )
    final = world.region.coordinator().drive(run_id)
    assert final["state"] == "succeeded", final["result"]
    puts = [c for c in world.net.writes() if c.method == "PUT"]
    assert len(puts) == 1 and puts[0].path.endswith("/contents/answers/badges.md")
    receipt = final["result"]["receipt"]
    head = world.github.head("acme/handbook", "harness/answers")
    assert receipt["downstream_id"] == f"acme/handbook@{head.sha}:answers/badges.md"
    assert receipt["operation_id"] in head.message
    delivered = head.files["answers/badges.md"].decode()
    assert delivered == world.text(final["result"]["candidates"]["primary"])
    assert world.github.head("acme/handbook", "main").files.get("answers/badges.md") is None
    approved = events(world.region, run_id, "effect.approved")
    assert approved and approved[0]["data"]["by"] == "ops"


@pytest.mark.parametrize("point", ["after_sink_commit", "after_receipt"])
def test_a_crash_after_the_commit_reconciles_from_github_and_never_commits_twice(world, point):
    from pheasant_harness import faults

    reg = world.handbook(
        actions=["list", "read", "write"],
        write_resources=["acme/handbook@harness/*:answers/*"],
        approval={"write": "none"},
    )
    target = "connection://gh/acme/handbook/answers/badges.md?branch=harness/answers"
    run_id = world.region.start(
        **answer_request([reg], destination={"adapter": "connection", "target": target})
    )["run_id"]
    with faults.armed(point), pytest.raises(faults.SimulatedCrash):
        world.region.coordinator("worker-a").drive(run_id)
    world.harness.runs.expire_lease(run_id)
    final = world.region.coordinator("worker-b").drive(run_id)
    assert final["state"] == "succeeded", final["result"]
    assert len([c for c in world.net.writes() if c.method == "PUT"]) == 1
    history = [
        c
        for c in world.github.repos["acme/handbook"]["commits"].values()
        if "pheasant harness delivery" in c.message
    ]
    assert len(history) == 1


def test_a_github_file_changed_after_validation_is_a_conflict_not_an_overwrite(world):
    reg = world.handbook(
        actions=["list", "read", "write"],
        write_resources=["acme/handbook@main:answers/*"],
        approval={"write": "none"},
    )
    world.github.commit(
        "acme/handbook", "main", {"answers/badges.md": "a teammate's answer\n"}, "teammate"
    )
    target = "connection://gh/acme/handbook/answers/badges.md?branch=main"
    final = world.region.run(
        **answer_request(
            [reg],
            destination={
                "adapter": "connection",
                "target": target,
                "expected_target_version": "absent",
            },
        )
    )
    assert final["state"] == "partial" and "changed after validation" in final["result"]["reason"]
    assert (
        world.github.head("acme/handbook", "main").files["answers/badges.md"]
        == b"a teammate's answer\n"
    )
    assert not [c for c in world.net.writes() if c.method == "PUT"]


def test_a_drive_delivery_is_tagged_and_reconciled_by_its_operation_id(world):
    from pheasant_harness import faults

    world.allow(
        "drive",
        ["list", "read", "write"],
        [f"{world.policies_folder}/*"],
        [f"{world.policies_folder}/answers-*"],
        approval={"write": "none"},
    )
    reg = world.register(f"connection://drive/{world.policies_folder}")
    target = f"connection://drive/{world.policies_folder}/answers-refunds.md"
    run_id = world.region.start(
        **answer_request(
            [reg],
            question="When are refunds approved automatically?",
            identifiers=["30 days"],
            destination={"adapter": "connection", "target": target},
        )
    )["run_id"]
    with faults.armed("after_sink_commit"), pytest.raises(faults.SimulatedCrash):
        world.region.coordinator("worker-a").drive(run_id)
    world.harness.runs.expire_lease(run_id)
    final = world.region.coordinator("worker-b").drive(run_id)
    assert final["state"] == "succeeded", final["result"]
    uploads = [c for c in world.net.writes() if "/upload/" in c.path]
    assert len(uploads) == 1
    created = [f for f in world.drive.files.values() if f.name == "answers-refunds.md"]
    assert len(created) == 1
    assert (
        created[0].app_properties["pheasantOperation"] == final["result"]["receipt"]["operation_id"]
    )
    assert "30 days" in created[0].content.decode()


def test_a_code_change_from_a_github_repo_is_tested_in_a_sandbox_that_holds_no_credential(
    world, monkeypatch
):
    world.allow("gh", ["list", "read"], ["acme/erp:*"])
    reg = world.register("connection://gh/acme/erp")
    final = world.region.run(
        workload="code_change",
        question="Show an exception badge on orders whose discount exceeds the policy threshold.",
        inputs=[reg],
        symbols=["badges"],
        proposals=[BADGE_EDIT],
        test_command=[*PYTEST],
        acceptance=[
            {
                "id": "sandbox-env",
                "type": "command",
                "required": False,
                "argv": ["python", "-c", "import os; print(sorted(os.environ))"],
            }
        ],
    )
    assert final["state"] == "succeeded", final["result"]
    detail = world.region.detail(final["run_id"])
    trees = [a for a in detail["artifacts"] if a["kind"] == "tree"]
    assert trees
    passed = {c["criterion_id"] for c in detail["verifications"] if c["outcome"] == "pass"}
    assert "builder-tests" in passed
    # The sandbox was built from bytes read through the connection, and no credential went in.
    assert leaks(world) == []


def test_a_recovery_after_a_teammate_edited_the_delivered_file_does_not_write_over_them(world):
    """The commit is found by its operation id, not by the file still holding our bytes."""
    from pheasant_harness import faults

    reg = world.handbook(
        actions=["list", "read", "write"],
        write_resources=["acme/handbook@harness/*:answers/*"],
        approval={"write": "none"},
    )
    target = "connection://gh/acme/handbook/answers/badges.md?branch=harness/answers"
    run_id = world.region.start(
        **answer_request([reg], destination={"adapter": "connection", "target": target})
    )["run_id"]
    with faults.armed("after_sink_commit"), pytest.raises(faults.SimulatedCrash):
        world.region.coordinator("worker-a").drive(run_id)
    world.github.commit(
        "acme/handbook",
        "harness/answers",
        {"answers/badges.md": "edited by a teammate\n"},
        "teammate",
    )
    world.harness.runs.expire_lease(run_id)
    final = world.region.coordinator("worker-b").drive(run_id)
    assert final["state"] == "succeeded", final["result"]
    assert len([c for c in world.net.writes() if c.method == "PUT"]) == 1
    head = world.github.head("acme/handbook", "harness/answers")
    assert head.files["answers/badges.md"] == b"edited by a teammate\n"
    assert final["result"]["receipt"]["disposition"].startswith("reconciled")


def test_two_identifiers_in_one_window_are_each_quoted_once_with_their_own_sentence(world):
    reg = world.handbook()
    final = world.region.run(
        **answer_request(
            [reg],
            question="Which orders need an exception badge, and since when?",
            identifiers=["exception badge", "Effective"],
        )
    )
    assert final["state"] == "succeeded", final["result"]
    findings = [
        line
        for line in world.text(final["result"]["candidates"]["primary"]).splitlines()
        if line.startswith("- `docs/discounts.md` lines")
    ]
    assert any("discount above 15% need an exception badge [E" in line for line in findings)
    assert any("Effective: 2026-10-01 [E" in line for line in findings)
