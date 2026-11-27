"""The harness as a workspace: attach files, pick a repository, branch and folder, deliver.

What a person does in the composer -- attach documents of any format, choose a
branch and a directory through a connection, commit a code change to a new
branch with a pull request, keep the result as a Pheasant artifact -- each run
end to end against the network-free fakes, with the receipts checked against
what the services actually hold.
"""
# ruff: noqa: E501, F811

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from tests.harness_connection_support import answer_request, leaks, world  # noqa: F401
from tests.harness_support import BADGE_EDIT, PYTEST, events

pytest.importorskip("pheasant_harness")

from pheasant_harness import attachments, faults, investigate, service  # noqa: E402
from pheasant_harness.common import HarnessError  # noqa: E402
from pheasant_harness.connections import ConnectionDenied  # noqa: E402

from pheasant.ingestion.pdf_write import text_pdf  # noqa: E402

DOCX = Path(__file__).parent / "fixtures" / "sample_workspace" / "documents" / "report.docx"
PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f"
    b"\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


def _files() -> list[tuple[str, bytes]]:
    return [
        ("brief/requirements.md", b"# Requirements\n\nThe badge reads Exception.\n"),
        (
            "brief/threshold.pdf",
            text_pdf([["Discount policy", "Orders with a discount above 15% need review."]]),
        ),
        ("brief/report.docx", DOCX.read_bytes()),
        ("brief/screen.png", PNG),
        ("scripts/check.py", b"print('ok')\n"),
    ]


# ---------------------------------------------------------------------------
# Attachments: any format in, pinned for the run, kept in Pheasant's uploads
# ---------------------------------------------------------------------------


def test_attached_files_of_several_formats_become_one_pinned_input(world):
    harness = world.harness
    attached = attachments.attach(harness, _files(), "local")
    by_path = {f["path"]: f for f in attached["files"]}
    # Documents get a text twin; code, Markdown and images stay as they are.
    assert by_path["brief/threshold.pdf.txt"]["derived_from"] == "brief/threshold.pdf"
    assert by_path["brief/report.docx.txt"]["derived_from"] == "brief/report.docx"
    assert by_path["brief/screen.png"]["binary"] is True
    assert "brief/requirements.md.txt" not in by_path
    reg = attached["registration"]["id"]
    registry = harness.registry
    revision = registry.resolve(reg, "local")
    assert not revision.time_bound and revision.kind == "attachment"
    resolver = registry.resolver(registry.get(reg))
    page = investigate.grep(resolver, revision, "discount above 15%")
    assert [m["path"] for m in page["matches"]] == ["brief/threshold.pdf.txt"]
    assert page["coverage"]["complete"] and page["coverage"]["binary_skipped"] >= 2
    # The originals are byte-identical; the digest is checked on every read.
    assert resolver.read_bytes(revision, "brief/report.docx") == DOCX.read_bytes()
    # Attaching the same files again is the same batch, the same registration.
    again = attachments.attach(harness, list(reversed(_files())), "local")
    assert again["registration"]["id"] == reg and again["registration"]["created"] is False


def test_attachments_land_in_pheasants_uploads_source_and_sync(world):
    services = world.region.services
    started: list[str] = []
    # The sync is started, not run here: a real background child would outlive the test.
    original = services.indexing
    object.__setattr__(
        services,
        "indexing",
        dataclasses.replace(original, start_sync=lambda name: started.append(name) or {}),
    )
    try:
        attached = attachments.attach(world.harness, _files(), "local", sync=True)
    finally:
        object.__setattr__(services, "indexing", original)
    uploads = attached["uploads"]
    assert uploads["source"] == "uploads" and uploads["landed"] == 5
    assert uploads["status"] == "syncing" and started == ["uploads"]
    assert uploads["folder"] == f"harness/{attached['batch']}"
    assert services.state.get_source("uploads")
    root = Path(uploads["directory"]) / uploads["folder"]
    assert (root / "brief" / "report.docx").read_bytes() == DOCX.read_bytes()
    # Indexed like any upload, so the attachment is knowledge too.
    world.region.tools.sync_source(world.region.config.knowledge_base_id, "uploads")
    hits = world.region.tools.search_context(
        world.region.config.knowledge_base_id,
        "discount above 15% need review",
        source_name="uploads",
    )["results"]
    assert any("threshold.pdf" in (h.get("relative_path") or "") for h in hits)


def test_attachment_names_cannot_escape_and_duplicates_must_agree(world):
    for bad in ("../etc/passwd", "a/../../b", "", "/"):
        with pytest.raises(HarnessError, match="not a relative file path"):
            attachments.safe_name(bad)
    with pytest.raises(HarnessError, match="two different files"):
        attachments.attach(world.harness, [("a.md", b"one"), ("a.md", b"two")], "local")
    assert attachments.safe_name("/folder//note.md") == "folder/note.md"


def test_a_run_reads_an_attached_pdf_and_cites_its_text(world):
    attached = attachments.attach(world.harness, _files(), "local", keep_in_uploads=False)
    assert attached["uploads"] == {"status": "not requested"}
    reg = attached["registration"]["id"]
    final = world.region.run(
        **answer_request([reg], question="What discount needs review?", identifiers=["need review"])
    )
    assert final["state"] == "succeeded", final["result"]
    detail = world.region.detail(final["run_id"])
    cited = [e for e in detail["evidence"] if e["method"] in ("read", "grep")]
    assert {e["locator"]["path"] for e in cited} == {"brief/threshold.pdf.txt"}
    assert all(e["revision"] == detail["revisions"][reg]["revision"] for e in cited)
    assert "discount above 15% need review" in world.text(final["result"]["candidates"]["primary"])


# ---------------------------------------------------------------------------
# Picking a branch and a directory through a connection
# ---------------------------------------------------------------------------


def test_browsing_a_repository_shows_its_branches_and_one_directory(world):
    world.github.commit("acme/handbook", "release/2026", {"docs/new.md": "# New\n"}, "release work")
    world.allow("gh", ["list", "read"], ["acme/handbook:*"])
    services = world.region.services
    usable = service.usable_connections(services)["connections"]
    gh = next(c for c in usable if c["name"] == "gh")
    assert gh["locations"] == ["acme/handbook"] and gh["actions"] == ["list", "read"]
    root = service.browse_connection(services, "gh", "acme/handbook")
    assert root["default_branch"] == "main" and root["branches"] == ["main", "release/2026"]
    assert {(e["name"], e["type"]) for e in root["entries"]} >= {
        ("docs", "dir"),
        ("README.md", "file"),
    }
    docs = service.browse_connection(services, "gh", "acme/handbook", "docs", "release/2026")
    assert "new.md" in {e["name"] for e in docs["entries"]} and docs["ref"] == "release/2026"
    assert {c["purpose"] for c in world.calls()} == {"browse"}
    # A repository the policy does not name cannot be browsed either.
    with pytest.raises(ConnectionDenied):
        service.browse_connection(services, "gh", "acme/erp")
    # And one with no policy offers nothing to pick.
    assert "drive" not in {c["name"] for c in service.usable_connections(services)["connections"]}


def test_browsing_drive_and_onedrive_folders(world):
    world.allow("drive", ["list", "read"], [f"{world.policies_folder}/*"])
    world.allow("m365", ["list", "read"], ["b!drive1:*"])
    services = world.region.services
    folder = service.browse_connection(services, "drive", world.policies_folder)
    assert {e["name"] for e in folder["entries"]} == {"Refund policy", "notes.md"}
    ops = service.browse_connection(services, "m365", "b!drive1", "Ops")
    assert {e["name"] for e in ops["entries"]} == {"oncall.md", "runbook.md"}


def test_a_registration_pinned_to_a_branch_and_a_subdirectory(world):
    repo = world.github.repos["acme/handbook"]
    repo["branches"]["feature"] = repo["branches"]["main"]  # forked from main
    world.github.commit(
        "acme/handbook",
        "feature",
        {"docs/discounts.md": "# Discounts\n\nOn the feature branch the threshold is 12%.\n"},
        "feature work",
    )
    world.allow("gh", ["list", "read"], ["acme/handbook:docs/*"])
    reg = world.harness.registry.register(
        "connection://gh/acme/handbook/docs", "local", scope={"ref": "feature"}
    )["id"]
    revision = world.harness.registry.resolve(reg, "local")
    assert revision.revision == world.github.head("acme/handbook", "feature").sha
    assert {e["path"] for e in revision.entries} == {"discounts.md", "shipping.md"}
    resolver = world.harness.registry.resolver(world.harness.registry.get(reg))
    assert "12%" in investigate.read_range(resolver, revision, "discounts.md")["content"]


# ---------------------------------------------------------------------------
# Committing a code change to a branch, with a pull request
# ---------------------------------------------------------------------------

TREE_TARGET = "connection://gh/acme/erp/?branch=harness/badge&base=main&pr=1"


def _erp(world, **policy) -> str:
    world.allow(
        "gh",
        ["list", "read", "write"],
        ["acme/erp:*"],
        policy.pop("write_resources", ["acme/erp@harness/*:*"]),
        approval={"write": "none"},
        **policy,
    )
    return world.register("connection://gh/acme/erp")


def _code_change(reg: str, target: str = TREE_TARGET) -> dict:
    return {
        "workload": "code_change",
        "question": "Show an exception badge on orders whose discount exceeds the policy threshold.",
        "inputs": [reg],
        "symbols": ["badges"],
        "proposals": [BADGE_EDIT],
        "test_command": PYTEST,
        "output": {
            "release_note": True,
            "release_note_title": "Exception badge on discounted orders",
        },
        "destination": {"adapter": "connection", "target": target},
    }


def test_a_code_change_is_one_commit_on_a_new_branch_with_a_pull_request(world):
    reg = _erp(world)
    main_before = world.github.head("acme/erp", "main").sha
    final = world.region.run(**_code_change(reg))
    assert final["state"] == "succeeded", final["result"]
    branch = world.github.head("acme/erp", "harness/badge")
    # One commit, on top of the base it was created from, changing exactly the edited file.
    assert branch.parent == main_before
    assert world.github.head("acme/erp", "main").sha == main_before
    before = world.github.repos["acme/erp"]["commits"][main_before].files
    changed = {p for p in set(branch.files) | set(before) if branch.files.get(p) != before.get(p)}
    assert changed == {"erp/orders.py"}
    assert b"Exception" in branch.files["erp/orders.py"]
    receipt = final["result"]["receipt"]
    assert receipt["operation_id"] in branch.message
    assert receipt["pull_request"] == "https://github.com/acme/erp/pull/1"
    assert receipt["files_changed"] == 1
    assert "Exception badge on discounted orders" in branch.message.splitlines()[0]
    pulls = world.github.repos["acme/erp"]["pulls"]
    assert len(pulls) == 1 and pulls[0]["head"] == "harness/badge" and pulls[0]["base"] == "main"
    assert "Exception badge" in pulls[0]["title"]
    # No contents write and no force: the Git Data path, fast-forward only.
    patches = [c for c in world.net.writes() if c.method == "PATCH"]
    assert patches and all(json.loads(c.body)["force"] is False for c in patches)
    assert not [c for c in world.net.writes() if c.method == "PUT"]


@pytest.mark.parametrize("point", ["after_sink_commit", "after_receipt"])
def test_a_crash_after_the_branch_commit_never_commits_or_opens_a_pr_twice(world, point):
    reg = _erp(world)
    run_id = world.region.start(**_code_change(reg))["run_id"]
    with faults.armed(point), pytest.raises(faults.SimulatedCrash):
        world.region.coordinator("worker-a").drive(run_id)
    world.harness.runs.expire_lease(run_id)
    final = world.region.coordinator("worker-b").drive(run_id)
    assert final["state"] == "succeeded", final["result"]
    history = world.github.history("acme/erp", "harness/badge")
    assert len([c for c in history if "pheasant harness delivery" in c.message]) == 1
    assert len(world.github.repos["acme/erp"]["pulls"]) == 1


def test_a_commit_touching_a_path_outside_the_write_resources_sends_nothing(world):
    reg = _erp(world, write_resources=["acme/erp@harness/*:", "acme/erp@harness/*:docs/*"])
    final = world.region.run(**_code_change(reg))
    assert final["state"] == "partial" and "erp/orders.py" in final["result"]["reason"]
    assert world.net.writes() == []
    assert "harness/badge" not in world.github.repos["acme/erp"]["branches"]


def test_a_branch_that_moved_is_never_forced(world):
    reg = _erp(world)
    world.github.commit(
        "acme/erp", "harness/badge", {"NOTES.md": "a teammate's commit\n"}, "teammate"
    )
    teammate = world.github.head("acme/erp", "harness/badge").sha
    run_id = world.region.start(**_code_change(reg))["run_id"]
    original = world.github.handle

    def racing(call):
        # Between reading the head and moving the ref, someone pushes.
        if call.method == "PATCH":
            world.github.commit(
                "acme/erp", "harness/badge", {"RACE.md": "pushed meanwhile\n"}, "race"
            )
        return original(call)

    world.github.handle = racing
    final = world.region.coordinator().drive(run_id)
    assert final["state"] == "partial" and "was not forced" in final["result"]["conflict"]
    history = [c.message for c in world.github.history("acme/erp", "harness/badge")]
    assert history[0] == "race" and teammate in {
        c.sha for c in world.github.history("acme/erp", "harness/badge")
    }
    assert not any("pheasant harness delivery" in m for m in history)


def test_a_document_delivered_to_a_branch_that_does_not_exist_yet_creates_it_from_base(world):
    reg = world.handbook(
        actions=["list", "read", "write"],
        write_resources=["acme/handbook@harness/*:answers/*"],
        approval={"write": "none"},
    )
    target = "connection://gh/acme/handbook/answers/badges.md?branch=harness/new&base=main"
    final = world.region.run(
        **answer_request([reg], destination={"adapter": "connection", "target": target})
    )
    assert final["state"] == "succeeded", final["result"]
    created = [c for c in world.net.writes() if c.path.endswith("/git/refs")]
    assert len(created) == 1
    head = world.github.head("acme/handbook", "harness/new")
    assert head.parent == world.github.head("acme/handbook", "main").sha
    assert "answers/badges.md" in head.files


# ---------------------------------------------------------------------------
# Keeping the result as a Pheasant artifact
# ---------------------------------------------------------------------------


def test_a_document_is_kept_as_a_pheasant_artifact_once(world):
    reg = world.handbook()
    run_id = world.region.start(
        **answer_request(
            [reg], destination={"adapter": "artifact", "target": "Exception badge rules"}
        )
    )["run_id"]
    with faults.armed("after_sink_commit"), pytest.raises(faults.SimulatedCrash):
        world.region.coordinator("worker-a").drive(run_id)
    world.harness.runs.expire_lease(run_id)
    final = world.region.coordinator("worker-b").drive(run_id)
    assert final["state"] == "succeeded", final["result"]
    rows = world.harness.state.rows("SELECT * FROM saved_artifacts")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == final["result"]["receipt"]["downstream_id"]
    assert row["title"] == "Exception badge rules" and row["kind"] == "report"
    assert row["session_id"] == final["result"]["receipt"]["operation_id"]
    payload = json.loads(row["payload_json"])
    assert "discount above 15% need an exception badge" in payload["answer"]


def test_a_code_change_is_kept_as_an_artifact_of_the_files_it_changed(world):
    world.allow("gh", ["list", "read"], ["acme/erp:*"])
    reg = world.register("connection://gh/acme/erp")
    request = _code_change(reg)
    request["destination"] = {"adapter": "artifact", "target": ""}
    final = world.region.run(**request)
    assert final["state"] == "succeeded", final["result"]
    row = world.harness.state.rows("SELECT * FROM saved_artifacts")[0]
    payload = json.loads(row["payload_json"])
    assert row["kind"] == "files" and [f["name"] for f in payload["files"]] == ["erp/orders.py"]
    assert "Exception badge on discounted orders" in row["title"]
    assert "Exception" in payload["files"][0]["content"]
    assert world.net.writes() == []


def test_the_workspace_flow_leaks_no_credential(world):
    attached = attachments.attach(world.harness, _files(), "local")
    reg = _erp(world)
    request = _code_change(reg)
    request["inputs"] = [reg, attached["registration"]["id"]]
    final = world.region.run(**request)
    assert final["state"] == "succeeded", final["result"]
    assert events(world.region, final["run_id"], "effect.committed")
    texts = [
        json.dumps(service.run_detail(world.region.services, final["run_id"]), default=str),
        json.dumps(service.usable_connections(world.region.services)),
        json.dumps(service.browse_connection(world.region.services, "gh", "acme/erp")),
    ]
    assert leaks(world, *texts) == []


def test_attachment_digest_is_checked_on_every_read(world):
    attached = attachments.attach(world.harness, _files(), "local", keep_in_uploads=False)
    reg = attached["registration"]["id"]
    registry = world.harness.registry
    revision = registry.resolve(reg, "local")
    entry = revision.entry("scripts/check.py")
    # A blob altered in storage is refused, never served as the attached file.
    world.harness.state.execute(
        "UPDATE harness_blobs SET content_b64=? WHERE digest=?",
        (__import__("base64").b64encode(b"print('tampered')\n").decode(), entry["blob"]),
    )
    resolver = registry.resolver(registry.get(reg))
    with pytest.raises(Exception, match="corrupt|do not match their digest"):
        resolver.read_bytes(revision, "scripts/check.py")


@pytest.mark.parametrize("point", ["before_pull_request", "after_sink_commit"])
def test_a_pull_request_is_opened_exactly_once_whatever_instant_the_worker_dies(world, point):
    """Between the commit and the pull request, or after both: one commit, one PR."""
    reg = _erp(world)
    run_id = world.region.start(**_code_change(reg))["run_id"]
    with faults.armed(point), pytest.raises(faults.SimulatedCrash):
        world.region.coordinator("worker-a").drive(run_id)
    assert len(world.github.repos["acme/erp"]["pulls"]) == (
        0 if point == "before_pull_request" else 1
    )
    world.harness.runs.expire_lease(run_id)
    final = world.region.coordinator("worker-b").drive(run_id)
    assert final["state"] == "succeeded", final["result"]
    assert len(world.github.repos["acme/erp"]["pulls"]) == 1
    assert final["result"]["receipt"]["pull_request"] == "https://github.com/acme/erp/pull/1"
    history = world.github.history("acme/erp", "harness/badge")
    assert len([c for c in history if "pheasant harness delivery" in c.message]) == 1


def test_a_recovered_commit_is_found_by_its_operation_even_after_a_teammate_edits_the_file(world):
    reg = _erp(world)
    run_id = world.region.start(**_code_change(reg))["run_id"]
    with faults.armed("after_sink_commit"), pytest.raises(faults.SimulatedCrash):
        world.region.coordinator("worker-a").drive(run_id)
    world.github.commit(
        "acme/erp", "harness/badge", {"erp/orders.py": "# a teammate rewrote this\n"}, "teammate"
    )
    world.harness.runs.expire_lease(run_id)
    final = world.region.coordinator("worker-b").drive(run_id)
    assert final["state"] == "succeeded", final["result"]
    head = world.github.head("acme/erp", "harness/badge")
    assert (
        head.message == "teammate" and head.files["erp/orders.py"] == b"# a teammate rewrote this\n"
    )
    history = world.github.history("acme/erp", "harness/badge")
    assert len([c for c in history if "pheasant harness delivery" in c.message]) == 1
