from __future__ import annotations

import argparse

import pytest
from scripts import release_version
from scripts.release_version import (
    effective_release_bump,
    normalize_bump_selection,
    pr_release_selections,
    print_merged_pr_bump,
    release_base_version,
    release_options,
    render_prompt,
    selected_release_bump,
    strongest_bump,
)
from scripts.sync_version import project_version


def test_normalize_bump_selection_accepts_words_and_numbers() -> None:
    assert normalize_bump_selection("major") == "major"
    assert normalize_bump_selection("2") == "minor"
    assert normalize_bump_selection("/release patch") == "patch"
    assert normalize_bump_selection("semver: 3") == "patch"


def test_normalize_bump_selection_rejects_incidental_text() -> None:
    assert normalize_bump_selection("This PR includes a patch for docs.") is None


def test_selected_release_bump_ignores_bot_prompt() -> None:
    comments = [
        {"user": {"type": "Bot"}, "body": "patch"},
        {"user": {"type": "User"}, "body": "minor"},
    ]

    assert selected_release_bump(comments)[0] == "minor"  # type: ignore[index]


def test_effective_release_bump_defaults_to_patch() -> None:
    selection, defaulted = effective_release_bump([], {"patch": "1.2.4", "minor": "1.3.0"})

    assert selection == "patch"
    assert defaulted is True


def test_effective_release_bump_comment_overrides_patch_default() -> None:
    comments = [{"user": {"type": "User"}, "body": "2"}]
    selection, defaulted = effective_release_bump(
        comments,
        {"patch": "1.2.4", "minor": "1.3.0"},
    )

    assert selection == "minor"
    assert defaulted is False


def test_render_prompt_discloses_patch_default() -> None:
    rendered = render_prompt({"patch": "1.2.4"}, "patch", defaulted=True)

    assert "Patch is selected by default" in rendered
    assert "`3` / `patch` (default) -> `1.2.4`" in rendered


def test_release_options_are_incremented_from_highest_existing_tag(monkeypatch) -> None:
    monkeypatch.setattr(release_version, "project_version", lambda: "1.2.0")
    assert release_options({"1.2.3", "latest", "sha-deadbeef"}) == {
        "major": "2.0.0",
        "minor": "1.3.0",
        "patch": "1.2.4",
    }


# --------------------------------------------------------------------------
# The increment is anchored to whichever is further ahead
# --------------------------------------------------------------------------


def test_the_base_version_follows_the_registry_when_it_is_ahead() -> None:
    """A published image main never recorded must not be handed out twice.

    This is the direction that happens when the release commit fails after the
    images are pushed — the order the publish workflow deliberately runs in.
    """

    ahead = _bumped_major(project_version())
    assert release_base_version({ahead, "latest"}) == ahead
    assert ahead not in set(release_options({ahead, "latest"}).values())


def test_the_base_version_follows_pyproject_when_it_is_ahead() -> None:
    """And this is the direction a partially-failed older release left behind.

    pyproject ahead of the registry means a version was recorded but never
    published; the next release must step past it rather than reissue it.
    """

    behind = "0.0.1"
    assert release_base_version({behind, "latest"}) == project_version()


def test_the_first_release_of_a_package_has_no_published_tags() -> None:
    assert release_base_version(set()) == project_version()
    assert release_options(set())["patch"] != project_version()


def _bumped_major(version: str) -> str:
    major, _, _ = version.split(".")
    return f"{int(major) + 1}.0.0"


# --------------------------------------------------------------------------
# The increment covers everything the release makes newly live
# --------------------------------------------------------------------------


def test_the_strongest_increment_wins() -> None:
    assert strongest_bump(["patch", "minor", "patch"]) == "minor"
    assert strongest_bump(["patch", "major", "minor"]) == "major"
    assert strongest_bump(["patch"]) == "patch"
    assert strongest_bump([]) is None
    assert strongest_bump(["nonsense"]) is None


def _fake_github(monkeypatch, commits: dict[str, int | None], comments: dict[int, str]) -> None:
    """A repo where `commits` maps sha -> PR number (None for a release commit)."""

    def merged_pr_for_commit(_repo, sha, _token, _api):
        number = commits.get(sha)
        return None if number is None else {"number": number}

    def comments_for_pr(_repo, number, _token, _api):
        body = comments.get(number)
        return [{"user": {"type": "User"}, "body": body}] if body else []

    monkeypatch.setattr(release_version, "merged_pr_for_commit", merged_pr_for_commit)
    monkeypatch.setattr(release_version, "comments_for_pr", comments_for_pr)


def test_selections_skip_release_commits_and_deduplicate(monkeypatch) -> None:
    """`chore: release` commits carry no PR, and one PR can span two commits."""

    _fake_github(
        monkeypatch,
        commits={"a": 53, "b": 53, "c": None, "d": 52},
        comments={53: "patch", 52: "minor"},
    )

    selections = pr_release_selections("o/r", ["a", "b", "c", "d"], "t", "u")
    assert selections == {53: "patch", 52: "minor"}


def test_a_pr_with_no_comment_falls_back_to_patch(monkeypatch) -> None:
    _fake_github(monkeypatch, commits={"a": 53}, comments={})

    assert pr_release_selections("o/r", ["a"], "t", "u") == {53: "patch"}


def test_an_unpublished_minor_is_not_demoted_by_a_later_patch(monkeypatch, tmp_path) -> None:
    """The case this exists for.

    A red CI left #52 on main unpublished. #53 merges asking for the default
    patch, and the image it builds contains #52 as well — so the release must
    be the minor #52 asked for, not the patch #53 did.
    """

    monkeypatch.setattr(release_version, "project_version", lambda: "1.2.0")

    _fake_github(
        monkeypatch,
        commits={"head": 53, "release": None, "older": 52},
        comments={52: "minor"},
    )
    monkeypatch.setattr(
        release_version,
        "fetch_container_versions",
        lambda *_args, **_kwargs: [{"metadata": {"container": {"tags": ["1.2.3", "latest"]}}}],
    )

    output = tmp_path / "github_output"
    args = argparse.Namespace(
        repo="o/r",
        sha=["head", "release", "older"],
        owner="o",
        package="pheasant",
        token="t",
        api_url="u",
        github_output=str(output),
    )
    assert print_merged_pr_bump(args) == 0

    recorded = dict(
        line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if line
    )
    assert recorded["bump"] == "minor"
    assert recorded["version"] == "1.3.0"
    assert recorded["prs"] == "52,53"


def test_a_later_major_is_not_demoted_by_an_older_patch(monkeypatch, tmp_path) -> None:
    """The same rule in the other direction.

    Paired with the test above deliberately: commits arrive newest-first, so a
    rule of "first wins" and a rule of "last wins" each get one of these two
    cases right by accident. Only "strongest wins" gets both.
    """

    _fake_github(
        monkeypatch,
        commits={"head": 53, "older": 52},
        comments={53: "major", 52: "patch"},
    )
    monkeypatch.setattr(
        release_version,
        "fetch_container_versions",
        lambda *_args, **_kwargs: [{"metadata": {"container": {"tags": ["1.2.3"]}}}],
    )

    output = tmp_path / "github_output"
    args = argparse.Namespace(
        repo="o/r",
        sha=["head", "older"],
        owner="o",
        package="pheasant",
        token="t",
        api_url="u",
        github_output=str(output),
    )
    assert print_merged_pr_bump(args) == 0

    recorded = dict(
        line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if line
    )
    assert recorded["bump"] == "major"
    assert recorded["version"] == "2.0.0"


def test_a_release_with_no_pull_request_at_all_is_refused(monkeypatch) -> None:
    _fake_github(monkeypatch, commits={"release": None}, comments={})
    args = argparse.Namespace(
        repo="o/r",
        sha=["release"],
        owner="o",
        package="pheasant",
        token="t",
        api_url="u",
        github_output=None,
    )
    with pytest.raises(SystemExit, match="No pull request is associated"):
        print_merged_pr_bump(args)
