"""One-line setup over arbitrary targets, and one-line container hosting.

Acceptance:

1. Any target shape resolves to a source: folder, file, markdown notes,
   obsidian vault, git repo (local + remote URL), web URL, s3://,
   connector plugin name.
2. A glob or ``--split`` over a folder-of-folders yields one source per
   child directory, with de-duplicated names.
3. ``pheasant up a b c`` writes one config with all three sources, indexes
   them, and re-running it re-indexes nothing (idempotency spine).
4. ``pheasant host`` generates a compose file plus a container-view config
   whose paths are remapped into the image — without needing Docker.
5. The UI's ``/sources/quick-add`` is the same resolution path over HTTP.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pheasant.api.app import create_app
from pheasant.cli import main
from pheasant.config.loader import load_config
from pheasant.deployment.host import render_compose
from pheasant.targets import (
    ResolvedTarget,
    TargetError,
    expand_specs,
    is_git_url,
    resolve_target,
    resolve_targets,
)

SAMPLE_WORKSPACE = Path(__file__).resolve().parent / "fixtures" / "sample_workspace"


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / ".pheasant" / "sources", tmp_path / ".pheasant" / "external"


def _sync_counts(output: str) -> list[tuple[int, int]]:
    return [(int(a), int(b)) for a, b in re.findall(r"indexed=(\d+) skipped=(\d+)", output)]


# --------------------------------------------------------------------- detection


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/anthropics/claude-code",
        "https://www.github.com/apache/spark/tree/master/python",
        "https://github.com/apache/spark/tree/master",
        "git@github.com:owner/repo.git",
        "https://gitlab.com/group/project",
        "https://example.com/thing.git",
        "ssh://git@host/repo",
    ],
)
def test_git_urls_are_recognised(url: str) -> None:
    assert is_git_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.example.com/guide",
        "https://github.com/owner/repo/blob/main/README.md",  # a page, not a clone
        "https://example.com",
    ],
)
def test_non_git_urls_are_not_clones(url: str) -> None:
    assert is_git_url(url) is False


# --------------------------------------------------------------- GitHub token


def test_git_env_injects_a_github_token_for_https_github_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pheasant.targets import _git_env

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret123")
    monkeypatch.delenv("GH_TOKEN", raising=False)

    env = _git_env("https://github.com/owner/private-repo.git")
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    header = pairs["http.https://github.com/.extraheader"]
    assert header.startswith("AUTHORIZATION: basic ")
    import base64

    decoded = base64.b64decode(header.removeprefix("AUTHORIZATION: basic ")).decode()
    assert decoded == "x-access-token:ghp_secret123"
    # The raw token itself never appears verbatim anywhere in the env dict
    # (only its base64-of-"x-access-token:<token>" form) -- nothing here is
    # ever placed on a subprocess argv list, so it cannot appear in a `ps`
    # listing or leak into git's own error text either.
    assert "ghp_secret123" not in header


def test_git_env_prefers_github_token_over_gh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from pheasant.targets import _git_env

    monkeypatch.setenv("GITHUB_TOKEN", "primary-token")
    monkeypatch.setenv("GH_TOKEN", "fallback-token")
    env = _git_env("https://github.com/owner/repo")
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    import base64

    decoded = base64.b64decode(
        pairs["http.https://github.com/.extraheader"].removeprefix("AUTHORIZATION: basic ")
    ).decode()
    assert decoded == "x-access-token:primary-token"


def test_git_env_falls_back_to_gh_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from pheasant.targets import _git_env

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "gh-cli-token")
    env = _git_env("https://github.com/owner/repo")
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert "http.https://github.com/.extraheader" in pairs


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/owner/repo.git",  # different host -- token must not leak here
        "git@github.com:owner/repo.git",  # SSH already carries its own auth
        "ssh://git@github.com/owner/repo.git",
    ],
)
def test_git_env_never_injects_a_token_for_non_https_github_urls(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    from pheasant.targets import _git_env

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret123")
    env = _git_env(url)
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert "http.https://github.com/.extraheader" not in pairs


def test_git_env_with_no_token_set_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from pheasant.targets import _git_env

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    env = _git_env("https://github.com/owner/repo.git")
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert "http.https://github.com/.extraheader" not in pairs


def test_fetch_target_passes_the_clone_url_through_to_git_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Wiring regression guard: a token configured in the environment must
    actually reach the `git clone` subprocess, not just `_git_env` in
    isolation."""
    from pheasant.targets import ResolvedTarget, fetch_target

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret123")
    monkeypatch.setattr("pheasant.targets.shutil.which", lambda _name: "/usr/bin/git")

    captured: dict = {}

    class FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        Path(cmd[-1]).mkdir(parents=True)
        return FakeResult()

    monkeypatch.setattr("pheasant.targets.subprocess.run", fake_run)

    target = ResolvedTarget(
        name="private-repo",
        type="repository",
        path=str(tmp_path / "private-repo"),
        description="test",
        clone_url="https://github.com/owner/private-repo.git",
    )
    fetch_target(target)

    pairs = {
        captured["env"][f"GIT_CONFIG_KEY_{i}"]: captured["env"][f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(captured["env"]["GIT_CONFIG_COUNT"]))
    }
    assert "http.https://github.com/.extraheader" in pairs
    # The token must never appear as an argv element (visible in a `ps`/Task
    # Manager listing) -- only via the env-var-based git config mechanism.
    assert not any("ghp_secret123" in str(part) for part in captured["cmd"])
    assert captured["cmd"][1:6] == ["clone", "--quiet", "--depth", "1", "--no-tags"]


def test_fetch_target_retries_a_public_github_repository_without_a_rejected_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale token must not make an otherwise public URL uncloneable."""

    from pheasant.targets import ResolvedTarget, fetch_target

    monkeypatch.setenv("GITHUB_TOKEN", "stale-token")
    monkeypatch.setattr("pheasant.targets.shutil.which", lambda _name: "/usr/bin/git")
    calls: list[dict] = []

    class FailedResult:
        returncode = 128
        stderr = (
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
        )
        stdout = ""

    class SuccessfulResult:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs["env"]})
        if len(calls) == 1:
            return FailedResult()
        Path(cmd[-1]).mkdir(parents=True)
        return SuccessfulResult()

    monkeypatch.setattr("pheasant.targets.subprocess.run", fake_run)
    target = ResolvedTarget(
        name="public-repo",
        type="repository",
        path=str(tmp_path / "public-repo"),
        description="test",
        clone_url="https://github.com/owner/public-repo.git",
    )

    fetch_target(target)

    assert len(calls) == 2
    first_pairs = {
        calls[0]["env"][f"GIT_CONFIG_KEY_{i}"]: calls[0]["env"][f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(calls[0]["env"]["GIT_CONFIG_COUNT"]))
    }
    second_pairs = {
        calls[1]["env"][f"GIT_CONFIG_KEY_{i}"]: calls[1]["env"][f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(calls[1]["env"]["GIT_CONFIG_COUNT"]))
    }
    assert "http.https://github.com/.extraheader" in first_pairs
    assert "http.https://github.com/.extraheader" not in second_pairs


def test_managed_fetch_retries_a_public_github_repository_without_a_rejected_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale token also must not block a subsequent managed refresh."""

    from pheasant.targets import _run_git

    monkeypatch.setenv("GITHUB_TOKEN", "stale-token")
    calls: list[dict] = []

    class FailedResult:
        returncode = 128
        stderr = "fatal: Authentication failed"
        stdout = ""

    class SuccessfulResult:
        returncode = 0
        stderr = ""
        stdout = "fetched\n"

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": cmd, "env": kwargs["env"]})
        return FailedResult() if len(calls) == 1 else SuccessfulResult()

    monkeypatch.setattr("pheasant.targets.subprocess.run", fake_run)

    result = _run_git(
        tmp_path,
        ["fetch", "--prune", "origin"],
        "https://github.com/owner/public-repo.git",
    )

    assert result.stdout == "fetched\n"
    assert len(calls) == 2
    first_pairs = {
        calls[0]["env"][f"GIT_CONFIG_KEY_{i}"]: calls[0]["env"][f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(calls[0]["env"]["GIT_CONFIG_COUNT"]))
    }
    second_pairs = {
        calls[1]["env"][f"GIT_CONFIG_KEY_{i}"]: calls[1]["env"][f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(calls[1]["env"]["GIT_CONFIG_COUNT"]))
    }
    assert "http.https://github.com/.extraheader" in first_pairs
    assert "http.https://github.com/.extraheader" not in second_pairs


def test_local_shapes_are_classified(tmp_path: Path, roots) -> None:
    clone_root, workspace = roots
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# a", encoding="utf-8")
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    (mixed / "a.pdf").write_bytes(b"%PDF-")
    (mixed / "b.csv").write_text("x", encoding="utf-8")
    single = tmp_path / "one.md"
    single.write_text("# one", encoding="utf-8")

    def kind(path: Path) -> str:
        return resolve_target(str(path), clone_root=clone_root, workspace=workspace).type

    assert kind(vault) == "obsidian_vault"
    assert kind(repo) == "repository"
    assert kind(notes) == "markdown_folder"
    assert kind(mixed) == "document_folder"
    assert kind(single) == "single_file"


def test_document_folder_target_emits_broad_include(tmp_path: Path, roots) -> None:
    clone_root, workspace = roots
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    (mixed / "agreement.pdf").write_bytes(b"%PDF-")

    target = resolve_target(str(mixed), clone_root=clone_root, workspace=workspace)

    assert target.type == "document_folder"
    assert target.to_source_dict()["include"] == ["**/*"]


def test_remote_and_connector_targets_resolve_without_touching_disk(roots) -> None:
    clone_root, workspace = roots

    repo = resolve_target(
        "https://github.com/owner/proj", clone_root=clone_root, workspace=workspace
    )
    assert repo.type == "repository"
    assert repo.clone_url == "https://github.com/owner/proj"
    assert repo.name == "proj"
    assert repo.path.endswith("proj")
    assert repo.to_source_dict()["repo"] == {
        "clone_url": "https://github.com/owner/proj",
        "clone_path": repo.clone_path,
        "clone_ref": None,
    }

    subtree = resolve_target(
        "https://github.com/apache/spark/tree/master/python",
        clone_root=clone_root,
        workspace=workspace,
    )
    assert subtree.type == "repository"
    assert subtree.clone_url == "https://github.com/apache/spark"
    assert subtree.clone_ref == "master"
    assert subtree.clone_path is not None and subtree.clone_path.endswith("spark")
    assert Path(subtree.path).parts[-2:] == ("spark", "python")
    assert Path(subtree.to_source_dict()["path"]).parts[-2:] == ("spark", "python")

    branch_root = resolve_target(
        "https://www.github.com/apache/spark/tree/master",
        clone_root=clone_root,
        workspace=workspace,
    )
    assert branch_root.clone_url == "https://github.com/apache/spark"
    assert branch_root.clone_ref == "master"
    assert branch_root.path.endswith("spark")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_existing_remote_clone_fast_forwards_before_reuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A second materialization must advance HEAD, not merely fetch refs."""

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    from pheasant.targets import fetch_target, managed_repository_state

    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    seed.mkdir()
    _git(seed, "init")
    _git(seed, "config", "user.email", "tests@example.com")
    _git(seed, "config", "user.name", "Pheasant tests")
    (seed / "README.md").write_text("one\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "one")
    _git(seed, "branch", "-M", "main")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")

    # Production rejects local-path clone transports. This test substitutes
    # one only to exercise real git fetch/merge behavior without a network.
    monkeypatch.setattr("pheasant.targets.validate_clone_url", lambda url: url)
    monkeypatch.setattr("pheasant.targets._git_env", lambda _url=None: dict(os.environ))
    target = ResolvedTarget(
        name="managed",
        type="repository",
        path=str(checkout),
        description="test",
        clone_url=str(remote),
        clone_path=str(checkout),
        clone_ref="main",
    )
    fetch_target(target)
    first = _git(checkout, "rev-parse", "HEAD")

    (seed / "README.md").write_text("two\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "two")
    _git(seed, "push")
    second = _git(seed, "rev-parse", "HEAD")

    fetch_target(target)

    assert first != second
    assert _git(checkout, "rev-parse", "HEAD") == second
    assert (checkout / "README.md").read_text(encoding="utf-8") == "two\n"
    assert managed_repository_state(target)["fresh"] is True


def test_managed_remote_refuses_to_index_a_dirty_or_ahead_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    from pheasant.targets import fetch_target

    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    seed.mkdir()
    _git(seed, "init")
    _git(seed, "config", "user.email", "tests@example.com")
    _git(seed, "config", "user.name", "Pheasant tests")
    (seed / "README.md").write_text("remote\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "branch", "-M", "main")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    monkeypatch.setattr("pheasant.targets.validate_clone_url", lambda url: url)
    monkeypatch.setattr("pheasant.targets._git_env", lambda _url=None: dict(os.environ))
    target = ResolvedTarget(
        name="managed",
        type="repository",
        path=str(checkout),
        description="test",
        clone_url=str(remote),
        clone_path=str(checkout),
        clone_ref="main",
    )
    fetch_target(target)
    (checkout / "README.md").write_text("local edit\n", encoding="utf-8")

    with pytest.raises(TargetError, match="working-tree changes"):
        fetch_target(target)

    _git(checkout, "config", "user.email", "tests@example.com")
    _git(checkout, "config", "user.name", "Pheasant tests")
    _git(checkout, "add", "README.md")
    _git(checkout, "commit", "-m", "local-only")
    with pytest.raises(TargetError, match="ahead of or diverged"):
        fetch_target(target)


def test_sync_records_remote_checkout_and_indexed_commit_equality(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    from pheasant.config.schema import PheasantConfig
    from pheasant.registry.source_registry import SourceRegistry
    from pheasant.sync.engine import SyncEngine

    checkout = tmp_path / "managed"
    checkout.mkdir()
    _git(checkout, "init")
    _git(checkout, "config", "user.email", "tests@example.com")
    _git(checkout, "config", "user.name", "Pheasant tests")
    (checkout / "README.md").write_text("managed content\n", encoding="utf-8")
    _git(checkout, "add", "README.md")
    _git(checkout, "commit", "-m", "initial")
    commit = _git(checkout, "rev-parse", "HEAD")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "managed-test",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [
                {
                    "name": "managed",
                    "type": "repository",
                    "path": str(checkout),
                    "include": ["**/*.md"],
                    "repo": {
                        "clone_url": "https://github.com/example/managed",
                        "clone_path": str(checkout),
                        "clone_ref": "main",
                    },
                }
            ],
        }
    )
    calls: list[str] = []

    def refreshed(source):
        calls.append(source.name)
        return {
            "managed": True,
            "remote_url": source.repo.clone_url,
            "requested_ref": source.repo.clone_ref,
            "tracking_ref": "refs/remotes/origin/main",
            "branch": "main",
            "local_commit": commit,
            "remote_commit": commit,
            "fresh": True,
        }

    monkeypatch.setattr("pheasant.targets.refresh_managed_repository", refreshed)
    engine = SyncEngine(config)
    try:
        result = engine.sync_source("managed", "full")
        listed = SourceRegistry(config, engine.state).list_sources()
    finally:
        engine.close()

    assert calls == ["managed"]
    assert result.details["repository"]["indexed_commit"] == commit
    assert result.details["repository"]["fresh"] is True
    assert result.details["checkpoint"]["high_watermark"]["repository"]["fresh"] is True
    assert listed[0]["repository"]["indexed_commit"] == commit
    assert listed[0]["repository"]["fresh"] is True


def test_remote_update_failure_marks_source_and_aborts_before_indexing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pheasant.config.schema import PheasantConfig
    from pheasant.sync.engine import SyncEngine

    checkout = tmp_path / "managed"
    checkout.mkdir()
    (checkout / "README.md").write_text("stale content\n", encoding="utf-8")
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "managed-failure-test",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(tmp_path),
                "exports_path": str(tmp_path / "exports"),
            },
            "sources": [
                {
                    "name": "managed",
                    "type": "repository",
                    "path": str(checkout),
                    "repo": {
                        "clone_url": "https://github.com/example/managed",
                        "clone_path": str(checkout),
                    },
                }
            ],
        }
    )

    def fail(_source):
        raise TargetError("authentication failed")

    monkeypatch.setattr("pheasant.targets.refresh_managed_repository", fail)
    engine = SyncEngine(config)
    try:
        with pytest.raises(TargetError, match="authentication failed"):
            engine.sync_source("managed", "incremental")
        row = engine.state.get_source("managed")
        artifacts = engine.state.rows("SELECT id FROM artifacts WHERE source_id=?", ("managed",))
    finally:
        engine.close()

    assert row is not None and row["last_status"] == "remote_error"
    assert artifacts == []


def test_github_subtree_rejects_encoded_path_traversal(roots) -> None:
    clone_root, workspace = roots
    with pytest.raises(TargetError, match="subpath"):
        resolve_target(
            "https://github.com/owner/repo/tree/main/%2e%2e/private",
            clone_root=clone_root,
            workspace=workspace,
        )

    web = resolve_target(
        "https://docs.example.com/guide", clone_root=clone_root, workspace=workspace
    )
    assert web.type == "web_collection"
    assert web.urls == ["https://docs.example.com/guide"]
    assert web.local is False

    bucket = resolve_target("s3://my-bucket/prefix", clone_root=clone_root, workspace=workspace)
    assert bucket.type == "s3"
    assert bucket.local is False

    # Step 31.1 plugin types pass through by name and resolve at dispatch.
    notion = resolve_target("notion:my-workspace", clone_root=clone_root, workspace=workspace)
    assert notion.type == "notion"
    assert notion.local is False


def test_explicit_type_prefix_overrides_detection(tmp_path: Path, roots) -> None:
    clone_root, workspace = roots
    folder = tmp_path / "stuff"
    folder.mkdir()
    (folder / "a.md").write_text("# a", encoding="utf-8")

    target = resolve_target(f"docs:{folder}", clone_root=clone_root, workspace=workspace)
    assert target.type == "document_folder"  # not the auto-detected markdown_folder


def test_missing_local_path_is_an_error(tmp_path: Path, roots) -> None:
    clone_root, workspace = roots
    with pytest.raises(TargetError):
        resolve_target(str(tmp_path / "nope"), clone_root=clone_root, workspace=workspace)


# ------------------------------------------------------------------- collections


def test_glob_and_split_expand_a_folder_of_folders(tmp_path: Path, monkeypatch) -> None:
    parent = tmp_path / "clients"
    for name in ("acme", "globex", "initech"):
        (parent / name).mkdir(parents=True)
        (parent / name / "brief.md").write_text("# brief", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    globbed = expand_specs(["clients/*"])
    assert len(globbed) == 3

    split = expand_specs([str(parent)], split=True)
    assert sorted(Path(p).name for p in split) == ["acme", "globex", "initech"]


def test_duplicate_names_are_disambiguated(tmp_path: Path, roots) -> None:
    clone_root, workspace = roots
    for parent in ("a", "b"):
        (tmp_path / parent / "docs").mkdir(parents=True)
        (tmp_path / parent / "docs" / "x.md").write_text("# x", encoding="utf-8")

    targets = resolve_targets(
        [str(tmp_path / "a" / "docs"), str(tmp_path / "b" / "docs")],
        clone_root=clone_root,
        workspace=workspace,
    )
    assert [t.name for t in targets] == ["docs", "docs-2"]


# --------------------------------------------------------------------- `up` e2e


def test_up_indexes_multiple_targets_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = tmp_path / "notes"
    shutil.copytree(SAMPLE_WORKSPACE, notes)
    clients = tmp_path / "clients"
    for name in ("acme", "globex"):
        (clients / name).mkdir(parents=True)
        (clients / name / "brief.md").write_text(f"# {name} brief\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main(["up", str(notes), str(clients / "*"), "--no-serve"]) == 0
    output = capsys.readouterr().out

    config_path = tmp_path / "pheasant.yaml"
    cfg = load_config(config_path)
    assert [s.name for s in cfg.sources] == ["notes", "acme", "globex"]
    assert cfg.sources[1].type.value == "markdown_folder"
    # Every local target is reachable under the security allowlist.
    roots = {str(p) for p in cfg.security.allow_workspace_roots}
    assert str(clients / "acme") in roots

    counts = _sync_counts(output)
    assert len(counts) == 3
    assert all(indexed > 0 for indexed, _ in counts)

    first_bytes = config_path.read_bytes()
    assert main(["up", str(notes), str(clients / "*"), "--no-serve"]) == 0
    second = capsys.readouterr().out
    assert config_path.read_bytes() == first_bytes, "config is never rewritten"
    assert all(indexed == 0 and skipped > 0 for indexed, skipped in _sync_counts(second))


def test_up_splits_a_parent_directory_into_one_source_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parent = tmp_path / "projects"
    for name in ("alpha", "beta"):
        (parent / name).mkdir(parents=True)
        (parent / name / "readme.md").write_text(f"# {name}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert main(["up", str(parent), "--split", "--no-serve"]) == 0
    capsys.readouterr()
    cfg = load_config(tmp_path / "pheasant.yaml")
    assert [s.name for s in cfg.sources] == ["alpha", "beta"]


def test_up_rejects_a_target_that_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["up", str(tmp_path / "missing"), "--no-serve"]) == 1


def test_generated_multi_source_config_round_trips_through_yaml(tmp_path: Path, roots) -> None:
    """Globs and URLs in a generated config must survive the YAML round trip."""
    from pheasant.quickstart import render_up_config

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# a", encoding="utf-8")
    clone_root, workspace = roots
    targets = resolve_targets(
        [str(notes), "https://docs.example.com/guide"],
        clone_root=clone_root,
        workspace=workspace,
    )
    config_path = tmp_path / "pheasant.yaml"

    text = render_up_config(targets, config_path)
    assert text == render_up_config(targets, config_path), "rendering is deterministic"

    raw = yaml.safe_load(text)
    assert [s["name"] for s in raw["sources"]] == ["notes", "docs-example-com"]
    assert raw["sources"][0]["include"] == ["**/*.md", "**/*.markdown"]
    assert raw["sources"][1]["urls"] == ["https://docs.example.com/guide"]


# --------------------------------------------------------------------- `host`


def test_host_generates_compose_and_container_config_without_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# a\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    args = argparse.Namespace(
        path=[str(notes)],
        config="pheasant.yaml",
        name=None,
        port=9100,
        ui_port=9200,
        profile="quickstart",
        split=False,
        output="docker-compose.pheasant.yml",
        image=None,
        ui_image=None,
        no_ui=False,
        print_only=True,
    )
    from pheasant.deployment.host import host_stack

    assert host_stack(args) == 0
    capsys.readouterr()

    compose = yaml.safe_load((tmp_path / "docker-compose.pheasant.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    assert services["pheasant"]["ports"] == ["9100:8765"]
    assert services["pheasant-ui"]["ports"] == ["9200:80"]
    # The local source is mounted read-only at a stable container path.
    assert any(v.endswith("/sources/notes:ro") for v in services["pheasant"]["volumes"])
    # State survives `docker compose down`.
    assert any(v.endswith(":/state") for v in services["pheasant"]["volumes"])

    container_config = yaml.safe_load(
        (tmp_path / "pheasant.container.yaml").read_text(encoding="utf-8")
    )
    assert container_config["sources"][0]["path"] == "/sources/notes"
    assert container_config["pheasant"]["state_path"] == "/state"
    assert "/sources/notes" in container_config["security"]["allow_workspace_roots"]
    # Regression: the host-side pheasant.yaml pins server.host to 127.0.0.1
    # (quickstart profile) so bare `pheasant start` never answers on the LAN.
    # The *container* config must not inherit that -- uvicorn binding
    # loopback inside the container makes it unreachable from the
    # pheasant-ui sidecar over the compose network (502 from nginx), even
    # though the pheasant container's own healthcheck still passes because
    # it curls itself over that same loopback.
    host_config = yaml.safe_load((tmp_path / "pheasant.yaml").read_text(encoding="utf-8"))
    assert host_config["server"]["host"] == "127.0.0.1"
    assert container_config["server"]["host"] == "0.0.0.0"


def test_host_pins_the_ui_image_and_builds_it_from_a_checkout(tmp_path: Path) -> None:
    """The UI sidecar must resolve to a real image, and to the current one.

    `latest` is never re-pulled once Docker has it locally, which is how an
    upgraded stack keeps serving a months-old bundle; and a source checkout can
    be ahead of any published tag, so it gets a build context too.
    """
    from pheasant.deployment.host import DEFAULT_UI_IMAGE, local_ui_context
    from pheasant.version import __version__

    assert DEFAULT_UI_IMAGE == f"ghcr.io/esatt10/pheasant-ui:{__version__}"

    target = ResolvedTarget(
        name="notes", type="markdown_folder", path=str(tmp_path / "notes"), description="d"
    )
    kwargs = {
        "config_path": tmp_path / "c.yaml",
        "state_dir": tmp_path / ".pheasant",
        "port": 8765,
        "ui_port": 8080,
    }

    ui = yaml.safe_load(render_compose([target], **kwargs))["services"]["pheasant-ui"]
    assert ui["image"] == DEFAULT_UI_IMAGE
    assert "build" not in ui, "no build stanza without a checkout to build from"

    built = yaml.safe_load(render_compose([target], ui_build_context="/repo/ui", **kwargs))
    assert built["services"]["pheasant-ui"]["build"] == {"context": "/repo/ui"}

    # An explicit --ui-image is a deliberate choice of a published bundle.
    pinned = yaml.safe_load(
        render_compose([target], ui_image="my/ui:1.0", ui_build_context="/repo/ui", **kwargs)
    )
    assert pinned["services"]["pheasant-ui"]["image"] == "my/ui:1.0"
    assert "build" not in pinned["services"]["pheasant-ui"]

    # This repo IS a checkout, so the fallback resolves to its ui/ directory.
    assert local_ui_context() == str(Path(__file__).resolve().parents[1] / "ui")


def test_host_can_omit_the_ui_sidecar(tmp_path: Path) -> None:
    target = ResolvedTarget(
        name="notes", type="markdown_folder", path=str(tmp_path / "notes"), description="d"
    )
    compose = yaml.safe_load(
        render_compose(
            [target],
            config_path=tmp_path / "c.yaml",
            state_dir=tmp_path / ".pheasant",
            port=8765,
            ui_port=8080,
            include_ui=False,
        )
    )
    assert set(compose["services"]) == {"pheasant"}


# --------------------------------------------------------------------- HTTP


def test_quick_add_registers_and_syncs_a_pasted_path(loaded_config, tmp_path: Path) -> None:
    external = tmp_path / "pasted-notes"
    external.mkdir()
    (external / "note.md").write_text("# Pasted\nSome content.\n", encoding="utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    client = TestClient(create_app(config=loaded_config))

    response = client.post("/sources/quick-add", json={"target": str(external)})

    assert response.status_code == 200
    body = response.json()
    assert body["sources"][0]["name"] == "pasted-notes"
    assert body["sources"][0]["type"] == "markdown_folder"
    assert body["sync_results"][0]["indexed_artifacts"] == 1
    assert any(s["name"] == "pasted-notes" for s in client.get("/sources").json())


def test_quick_add_rejects_a_bad_target(loaded_config, tmp_path: Path) -> None:
    client = TestClient(create_app(config=loaded_config))
    response = client.post("/sources/quick-add", json={"target": str(tmp_path / "nowhere")})
    assert response.status_code == 400


def _wait_until_not_syncing(client: TestClient, name: str, timeout_s: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout_s
    record = None
    while time.monotonic() < deadline:
        record = next((s for s in client.get("/sources").json() if s["name"] == name), None)
        if record is not None and not record["syncing"]:
            return record
        time.sleep(0.2)
    raise AssertionError(f"{name} never finished background-syncing within {timeout_s}s")


def test_quick_add_with_wait_false_registers_immediately_and_syncs_in_background(
    loaded_config, config_path: Path, tmp_path: Path
) -> None:
    """The fix for the UI's "stuck at the form" / 504 report: registering a
    source must return before indexing finishes, and GET /sources must show
    live progress rather than the caller having to hold the connection open
    for however long the first sync takes."""

    external = tmp_path / "pasted-notes-bg"
    external.mkdir()
    (external / "note.md").write_text("# Pasted\nSome content.\n", encoding="utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    client = TestClient(create_app(config=loaded_config, config_path=config_path))

    response = client.post("/sources/quick-add", json={"target": str(external), "wait": False})

    assert response.status_code == 200
    body = response.json()
    assert body["sources"][0]["name"] == "pasted-notes-bg"
    # Nothing was awaited — no synchronous sync result yet.
    assert body["sync_results"] == []
    assert body["syncing"] == ["pasted-notes-bg"]

    # Visible as syncing immediately, before the background thread has
    # necessarily even started — this is what a client polls right after
    # the registration response to show a live "syncing" badge.
    immediate = next(s for s in client.get("/sources").json() if s["name"] == "pasted-notes-bg")
    assert immediate["syncing"] is True

    settled = _wait_until_not_syncing(client, "pasted-notes-bg")
    assert settled["sync_error"] is None


def test_sync_source_with_wait_false_returns_immediately_and_settles(
    loaded_config, config_path: Path, tmp_path: Path
) -> None:
    external = tmp_path / "pasted-notes-bg2"
    external.mkdir()
    (external / "note.md").write_text("# Pasted\nMore content.\n", encoding="utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    client.post("/sources/quick-add", json={"target": str(external), "sync_now": False})

    response = client.post("/sync/pasted-notes-bg2", json={"wait": False})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "syncing"
    assert payload["source_id"] == "pasted-notes-bg2"
    # The job id is the handle the UI follows for live progress.
    assert payload["job_id"]
    settled = _wait_until_not_syncing(client, "pasted-notes-bg2")
    assert settled["sync_error"] is None


def test_sync_source_with_wait_false_does_not_start_a_second_overlapping_sync(
    loaded_config, config_path: Path, tmp_path: Path
) -> None:
    """Regression: found live — a source with no checkpoint yet (every
    attempt does a full pass) got a background sync started twice in close
    succession (a container-startup trigger landing on top of a manual
    one), running two concurrent embedding-heavy passes over the same
    source. That tripped the OpenAI embeddings endpoint's rate limit and
    duplicated disk/CPU work for nothing. A second `wait: false` trigger
    for a source that is already syncing must be a no-op, not a second
    thread."""

    external = tmp_path / "pasted-notes-bg4"
    external.mkdir()
    (external / "note.md").write_text("# Pasted\nStill more content.\n", encoding="utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    client = TestClient(create_app(config=loaded_config, config_path=config_path))
    client.post("/sources/quick-add", json={"target": str(external), "sync_now": False})

    first = client.post("/sync/pasted-notes-bg4", json={"wait": False})
    second = client.post("/sync/pasted-notes-bg4", json={"wait": False})

    assert first.json()["status"] == "syncing"
    assert second.json() == {
        "status": "already_syncing",
        # No second job was created — the refusal is the point.
        "job_id": None,
        # Empty on this role: an `all`/`indexer` process indexes locally, so
        # nothing is published. The field exists because an `api` replica has
        # no local job to report and returns its queue task ids here instead.
        "queued_tasks": [],
        "source_id": "pasted-notes-bg4",
    }
    settled = _wait_until_not_syncing(client, "pasted-notes-bg4")
    assert settled["sync_error"] is None


def test_sync_source_with_wait_false_rejects_an_unknown_source(loaded_config) -> None:
    client = TestClient(create_app(config=loaded_config))
    response = client.post("/sync/does-not-exist", json={"wait": False})
    assert response.status_code == 404


def test_sync_source_with_wait_false_accepts_a_source_known_only_to_the_state_registry(
    loaded_config, config_path: Path, tmp_path: Path
) -> None:
    """Regression: a source registered at runtime (quick-add) lives in the
    state registry immediately, but only lands in *this process's*
    `config.sources` list. A second process reading the same `/state` —
    the sync worker, or (as happened live) the API server after a
    container restart — starts with a `config.sources` that has never
    heard of it; only `SyncEngine._source`'s state-registry fallback makes
    it syncable there. The `wait=false` validation must check the same
    place `_source` ultimately does, not just `config.sources`, or a
    perfectly good source 404s the moment a fresh process is asked to
    sync it in the background."""

    external = tmp_path / "pasted-notes-bg3"
    external.mkdir()
    (external / "note.md").write_text("# Pasted\nEven more content.\n", encoding="utf-8")
    loaded_config.security.allow_user_selected_source_paths = True
    first_process = TestClient(create_app(config=loaded_config, config_path=config_path))
    first_process.post("/sources/quick-add", json={"target": str(external), "sync_now": False})

    # A fresh config load, same `/state` on disk — a new process's starting
    # point, with an empty `config.sources` for anything not in the YAML.
    fresh_config = load_config(config_path)
    assert not any(s.name == "pasted-notes-bg3" for s in fresh_config.sources)
    second_process = TestClient(create_app(config=fresh_config, config_path=config_path))

    response = second_process.post("/sync/pasted-notes-bg3", json={"wait": False})

    assert response.status_code == 200, response.json()
    settled = _wait_until_not_syncing(second_process, "pasted-notes-bg3")
    assert settled["sync_error"] is None


def test_overview_reports_whether_there_is_a_graph_to_show(loaded_config) -> None:
    """The lone-star-node case: a fresh KB must report has_content False."""
    app = create_app(config=loaded_config)
    client = TestClient(app)

    empty = client.get("/overview").json()
    assert empty["has_content"] is False
    assert empty["indexed_artifacts"] == 0

    app.state.engine.sync_source("architecture-notes", "full")
    filled = client.get("/overview").json()
    assert filled["has_content"] is True
    assert filled["indexed_artifacts"] > 0
    assert filled["node_counts"]["knowledge_base"] == 1


def test_graph_route_filters_noisy_node_types(loaded_config) -> None:
    app = create_app(config=loaded_config)
    app.state.engine.sync_source("architecture-notes", "full")
    client = TestClient(app)

    everything = client.get("/graph").json()
    trimmed = client.get("/graph", params={"exclude_types": "concept,chunk"}).json()

    assert len(trimmed["nodes"]) < len(everything["nodes"])
    assert not any(n.get("type") in ("concept", "chunk") for n in trimmed["nodes"])
    # Totals still describe the whole graph, so the UI can say "x of y".
    assert trimmed["total_nodes"] == everything["total_nodes"]
    assert trimmed["filtered"] is True


def test_mcp_info_exposes_the_tool_surface(loaded_config) -> None:
    client = TestClient(create_app(config=loaded_config))
    info = client.get("/mcp/info").json()

    names = {tool["name"] for tool in info["tools"]}
    assert {"search_context", "sync_source", "get_graph_neighbors"} <= names
    assert info["stdio_command"][:2] == ["pheasant", "mcp"]
