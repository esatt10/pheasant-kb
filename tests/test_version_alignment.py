"""The released version, and the container references that must follow it.

Two failure modes live here. One is a generated file drifting off
`pyproject.toml`. The other is subtler and is what these tests were extended
for: a published *reference* — a compose `image:`, a manifest, a `docker run`
line in the README — naming a tag the publish workflow never pushes. Both
surface only as a pull failure on someone else's machine.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from scripts.sync_version import (
    check_image_version_increment,
    check_image_version_published,
    managed_paths,
)

from pheasant.version import __version__
from tests.conftest import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def test_generated_version_references_match_pyproject() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/sync_version.py", "--check"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_image_version_check_rejects_duplicate_tag() -> None:
    with pytest.raises(SystemExit, match="already exists"):
        check_image_version_increment("1.2.3", {"1.2.3", "latest", "sha-deadbeef"})


def test_image_version_check_rejects_lower_or_equal_version() -> None:
    with pytest.raises(SystemExit, match="must be greater"):
        check_image_version_increment("1.2.3", {"v1.2.4"})


def test_image_version_check_accepts_next_higher_version() -> None:
    check_image_version_increment("1.3.0", {"v1.2.4", "latest", "sha-deadbeef"})


# --------------------------------------------------------------------------
# Published image references
# --------------------------------------------------------------------------

#: Any `ghcr.io/esatt10/pheasant[-ui]:<tag>` reference, tag captured.
IMAGE_REFERENCE_RE = re.compile(r"ghcr\.io/esatt10/(pheasant(?:-ui)?):([0-9A-Za-z._+-]+)")

#: Files that are applied, run or copied verbatim — as opposed to prose, which
#: is allowed to spell a tag as `<version>`. A wrong tag in one of these starts
#: the wrong image; there is no reader to notice.
MACHINE_CONSUMED = (
    "compose.yaml",
    ".env.example",
    "deploy/compose/.env.example",
    "deploy/compose/docker-compose.yml",
    "deploy/compose/docker-compose.advanced.yml",
    "deploy/compose/docker-compose.fresh.yml",
    "deploy/compose/docker-compose.scale.yml",
    "docker-compose.override.yml",
    "pheasant.example.yaml",
    "deploy/",
)


def _machine_consumed_files() -> list[Path]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.split("\0")
    return [
        REPO_ROOT / name
        for name in tracked
        if name and name.startswith(MACHINE_CONSUMED) and Path(name).suffix != ".md"
    ]


def test_every_runnable_file_names_the_current_image_version() -> None:
    """A pinned tag in a file someone runs must be the version they installed.

    The fresh Compose manifest sat on 0.9.0 for three releases because nothing
    rewrote it and nothing checked it: `--check` only inspected the handful of
    files `replacements()` happened to list, so a new compose file or manifest
    joined the repo already outside the net. This closes that by scanning the
    files themselves rather than the script's own list.
    """

    stale: list[str] = []
    scanned = 0
    for path in _machine_consumed_files():
        for image, tag in IMAGE_REFERENCE_RE.findall(path.read_text(encoding="utf-8")):
            scanned += 1
            if tag != __version__:
                stale.append(f"{path.relative_to(REPO_ROOT)}: {image}:{tag}")

    assert scanned, "no image references found — the scan is not looking where it should"
    assert not stale, (
        "these files pin an image tag that is not the current version "
        f"{__version__}: {stale}. Run: python scripts/sync_version.py --write"
    )


def test_sync_version_rewrites_every_reference_the_scan_finds() -> None:
    """The checker and the writer must cover the same files.

    Without this the scan above would fail on a file `--write` cannot fix, and
    the only remedy would be hand-editing the tag — which is the drift.
    """

    managed = set(managed_paths())
    referencing = {
        str(path.relative_to(REPO_ROOT))
        for path in _machine_consumed_files()
        if IMAGE_REFERENCE_RE.search(path.read_text(encoding="utf-8"))
    }

    unmanaged = referencing - managed
    assert not unmanaged, (
        f"these files pin a published image but sync_version.py does not rewrite them: "
        f"{sorted(unmanaged)}"
    )


# --------------------------------------------------------------------------
# The publish workflow
# --------------------------------------------------------------------------


def _publish_job() -> dict:
    workflow = yaml.safe_load((WORKFLOWS / "container.yml").read_text(encoding="utf-8"))
    return workflow["jobs"]["publish"]


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_the_release_commit_stages_every_file_the_release_rewrites() -> None:
    """A rewritten-but-unstaged file is left stale on main.

    The workflow used to name the files itself, in a list that had to be kept
    in step with `replacements()` by hand. It was not, and the next `--check`
    on main is where that surfaces — after the release has already been cut.
    Deriving the list from the script is the fix; this asserts it stays
    derived rather than being pasted back.
    """

    commit = _step(_publish_job(), "Commit release version to main")["run"]
    assert "sync_version.py --list-paths" in commit
    assert "git add" in commit

    staged = [line for line in commit.splitlines() if "git add" in line]
    assert len(staged) == 1, "more than one staging line; one of them is a hand-maintained list"
    assert not re.search(r"git add\s+[^|]*\.(?:toml|yml|yaml)", staged[0]), (
        f"the release still stages hand-written paths: {staged[0].strip()}"
    )


def test_every_image_tag_the_repository_references_is_actually_published() -> None:
    """`docker run ghcr.io/esatt10/pheasant` means `:latest`, and it must exist.

    The README's headline command, the setup guide's one-liner and the chat
    how-to all use the untagged form. Only the UI image had ever pushed a
    `latest`, so the flagship install command failed with `manifest unknown`
    against a registry that otherwise held every release.
    """

    job = _publish_job()
    pushed: set[str] = set()
    for step in job["steps"]:
        tags = (step.get("with") or {}).get("tags")
        if not tags:
            continue
        for tag in str(tags).splitlines():
            _, _, suffix = tag.strip().rpartition(":")
            if suffix:
                pushed.add(suffix)

    assert "latest" in pushed, "nothing pushes a latest tag"

    # Both images, not just the UI one.
    ref_lines = [
        line.strip()
        for step in job["steps"]
        for line in str((step.get("with") or {}).get("tags") or "").splitlines()
        if line.strip()
    ]
    assert any(line.endswith(":latest") and "ui_ref" in line for line in ref_lines)
    assert any(line.endswith(":latest") and "outputs.ref" in line for line in ref_lines)


def test_the_container_ci_job_installs_the_cli_it_runs() -> None:
    """`python -m pheasant` in CI needs pheasant installed.

    It ran without one only because a root `sitecustomize.py` put `src/` on
    `sys.path` for anything started from the checkout. Deleting that shim —
    correctly — broke this job, and the failure was a plain
    "No module named pheasant" in a job whose name says "container".
    """

    workflow = yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["container-build"]["steps"]
    scripts = [str(step.get("run", "")) for step in steps]

    runs_cli = next(i for i, run in enumerate(scripts) if "python -m pheasant" in run)
    installs = [i for i, run in enumerate(scripts) if re.search(r"pip install\b.*\s\.", run)]
    assert installs, "the container job never installs the package"
    assert min(installs) < runs_cli, "the package is installed after the step that needs it"


# --------------------------------------------------------------------------
# The release records only what the registry actually holds
# --------------------------------------------------------------------------


def test_published_check_accepts_a_version_that_is_live() -> None:
    check_image_version_published("1.2.3", {"1.2.3", "latest", "sha-deadbeef"})


def test_published_check_rejects_a_version_the_push_never_landed() -> None:
    with pytest.raises(SystemExit, match="missing published tag"):
        check_image_version_published("1.2.3", {"1.2.2", "latest"})


def test_published_check_rejects_a_missing_latest() -> None:
    """`latest` is the tag the README's untagged `docker run` resolves to."""

    with pytest.raises(SystemExit, match="missing published tag"):
        check_image_version_published("1.2.3", {"1.2.3"})


def test_the_release_commit_runs_only_after_the_images_are_live() -> None:
    """main pins the new version into files people apply directly.

    Recording it before the push has landed is what leaves every compose file
    and manifest on main naming an image that does not exist. Publishing
    without the commit is the recoverable direction: the next release
    increments from max(pyproject, highest published tag), so an unrecorded
    image is skipped rather than dangling.
    """

    names = [step.get("name") for step in _publish_job()["steps"]]
    commit = names.index("Commit release version to main")
    verify = names.index("Verify the published tags are live")

    for pushing in ("Push semver image", "Push UI image"):
        assert names.index(pushing) < commit, f"{pushing} must run before the release commit"
    assert verify < commit, "the registry must be read back before main records the version"


def test_the_release_commit_survives_main_moving_under_it() -> None:
    """The images are already live by then, so giving up is not an option.

    A dropped release commit leaves main naming the *previous* version while
    the registry holds a newer one that nothing references.
    """

    commit = _step(_publish_job(), "Commit release version to main")["run"]
    assert "git rebase origin/main" in commit
    assert "git fetch origin main" in commit


# --------------------------------------------------------------------------
# A merge always resolves to a container, or says why it did not
# --------------------------------------------------------------------------


def test_the_release_range_covers_every_commit_since_the_last_release() -> None:
    """One `--sha` means one PR, and that is the bug.

    A merge left unpublished by a red CI still ships in the next image, so the
    increment has to be resolved from every commit since the last recorded
    release — not from whichever PR merged most recently.
    """

    job = _publish_job()
    resolve = _step(job, "Resolve release increment")["run"]

    assert "chore: release " in resolve, "the range is not anchored to the last release"
    assert 'git log --format=%H "$range"' in resolve
    assert "sha_args" in resolve and "--sha" in resolve
    assert "workflow_run.head_sha" not in resolve, "still resolving from a single commit"

    checkout = _step(job, "Check out repository")
    assert checkout["with"]["fetch-depth"] == 0, "a depth-1 checkout cannot see the range"


def test_a_merge_that_publishes_nothing_is_not_a_silent_skip() -> None:
    """The job's `if:` used to swallow a red CI on main.

    A skipped workflow reads the same as one that had nothing to do, which is
    how a merge sat on main for days with no image anyone had published.
    """

    workflow = yaml.safe_load((WORKFLOWS / "container.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["publish"]

    assert "conclusion" not in str(job["if"]), "a failed CI still skips the job silently"
    assert "workflow_run.event" in str(job["if"]), "the job must stay scoped to main pushes"

    gate = _step(job, "Require a green CI run")["run"]
    assert "workflow_run.conclusion" in gate
    assert "exit 1" in gate, "an unpublished merge must fail rather than pass quietly"
    assert "GITHUB_STEP_SUMMARY" in gate, "say which commit is live without an image"
