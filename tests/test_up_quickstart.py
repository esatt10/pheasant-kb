"""``pheasant up`` — the one-command personal quickstart.

What it must guarantee:

1. In a fixture workspace, ``pheasant up --no-serve`` writes a config that
   ``load_config`` round-trips, indexes > 0 artifacts, and creates state
   under ``./.pheasant/state``.
2. A second run reuses the config byte-identically and re-indexes nothing
   (the idempotency spine, extended to the bootstrap path).
3. Source-type detection: ``.obsidian/`` → obsidian_vault, ``.git/`` →
   repository, else document_folder; an existing config is never rewritten.
4. Fully offline; no synapse config is emitted (standalone mode untouched).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

from pheasant.cli import main
from pheasant.config.loader import load_config
from pheasant.config.schema import SourceType
from pheasant.quickstart import detect_source_type, ensure_up_config, render_up_config

SAMPLE_WORKSPACE = Path(__file__).resolve().parent / "fixtures" / "sample_workspace"


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway copy of the sample workspace, with cwd at its parent."""
    ws = tmp_path / "my-notes"
    shutil.copytree(SAMPLE_WORKSPACE, ws)
    monkeypatch.chdir(tmp_path)
    return ws


def _sync_counts(output: str) -> tuple[int, int]:
    match = re.search(r"indexed=(\d+) skipped=(\d+)", output)
    assert match, f"no sync result line in output:\n{output}"
    return int(match.group(1)), int(match.group(2))


def test_detect_source_type(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert detect_source_type(vault) is SourceType.markdown_folder
    assert detect_source_type(repo) is SourceType.repository
    assert detect_source_type(plain) is SourceType.document_folder


def test_up_generates_config_indexes_and_lands_state_locally(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["up", str(workspace), "--no-serve"])
    assert rc == 0
    output = capsys.readouterr().out

    config_path = tmp_path / "pheasant.yaml"
    assert config_path.exists()
    cfg = load_config(config_path)
    assert cfg.pheasant.name == "my-notes"
    assert len(cfg.sources) == 1
    assert cfg.sources[0].type is SourceType.document_folder
    assert cfg.sources[0].path == workspace
    assert cfg.pheasant.state_path == tmp_path / ".pheasant" / "state"

    indexed, _ = _sync_counts(output)
    assert indexed > 0
    assert (tmp_path / ".pheasant" / "state" / "pheasant.db").exists()

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "synapse" not in raw or not raw["synapse"].get("router_url")


def test_up_rerun_is_idempotent(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["up", str(workspace), "--no-serve"]) == 0
    capsys.readouterr()
    config_path = tmp_path / "pheasant.yaml"
    first_bytes = config_path.read_bytes()

    assert main(["up", str(workspace), "--no-serve"]) == 0
    output = capsys.readouterr().out
    assert "Reusing existing" in output
    assert config_path.read_bytes() == first_bytes

    indexed, skipped = _sync_counts(output)
    assert indexed == 0
    assert skipped > 0


def test_up_never_rewrites_a_user_edited_config(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["up", str(workspace), "--no-serve"]) == 0
    capsys.readouterr()
    config_path = tmp_path / "pheasant.yaml"
    marker = "# user edit — must survive re-runs\n"
    config_path.write_text(marker + config_path.read_text(encoding="utf-8"), encoding="utf-8")

    assert main(["up", str(workspace), "--no-serve"]) == 0
    assert config_path.read_text(encoding="utf-8").startswith(marker)


def test_up_indexes_an_obsidian_vault_as_markdown_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = tmp_path / "Second Brain"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / "daily.md").write_text("# Daily\nA [[linked]] note.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = main(["up", str(vault), "--no-serve", "--port", "9999"])
    assert rc == 0
    cfg = load_config(tmp_path / "pheasant.yaml")
    assert cfg.pheasant.name == "second-brain"
    assert cfg.sources[0].type is SourceType.markdown_folder
    assert cfg.server.port == 9999
    indexed, _ = _sync_counts(capsys.readouterr().out)
    assert indexed > 0


def test_up_rejects_missing_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["up", str(tmp_path / "nope")]) == 1


def test_render_up_config_is_deterministic(tmp_path: Path) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    config_path = tmp_path / "pheasant.yaml"
    once = render_up_config(target, config_path)
    twice = render_up_config(target, config_path)
    assert once == twice
    assert ensure_up_config(target, config_path) is True
    assert ensure_up_config(target, config_path) is False


def test_up_adds_a_new_target_to_an_existing_config(tmp_path: Path) -> None:
    """Regression, found in a live run: `pheasant setup` writes a config with
    no sources and tells you to add one with `pheasant up <folder> -c
    <config>`. That silently indexed nothing — `up` treated any existing
    config as "reuse", so the target was dropped and the command reported
    success. A target that is not configured yet is appended."""
    import json

    from pheasant.cli import main
    from pheasant.config.loader import load_config

    # `pheasant.state_path` defaults to the container path `/state` — correct
    # inside Docker, but not something a test (or a laptop user running
    # `setup` outside a container, which the wizard's own blurb warns about)
    # should let `up` try to create. Scope it into tmp_path like any other
    # non-container run would.
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps(
            {
                "pheasant.state_path": str(tmp_path / "state"),
                "pheasant.exports_path": str(tmp_path / "exports"),
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "pheasant.yaml"
    assert (
        main(
            [
                "setup",
                "--accept-defaults",
                "--answers",
                str(answers),
                "-o",
                str(config_path),
            ]
        )
        == 0
    )
    assert load_config(config_path).sources == []

    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# A\n\nSome prose.\n", encoding="utf-8")

    assert main(["up", str(notes), "-c", str(config_path), "--no-serve"]) == 0

    cfg = load_config(config_path)
    assert [s.name for s in cfg.sources] == ["notes"]
    assert str(notes) in {str(p) for p in cfg.security.allow_workspace_roots}


def test_up_leaves_an_existing_config_byte_identical_when_nothing_is_new(
    tmp_path: Path,
) -> None:
    """The other half of the contract: your edits are never rewritten."""
    from pheasant.cli import main

    config_path = tmp_path / "pheasant.yaml"
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# A\n\nSome prose.\n", encoding="utf-8")

    main(["up", str(notes), "-c", str(config_path), "--no-serve"])
    before = config_path.read_bytes()
    main(["up", str(notes), "-c", str(config_path), "--no-serve"])
    assert config_path.read_bytes() == before


def test_up_does_not_add_the_same_directory_twice_under_two_names(
    tmp_path: Path,
) -> None:
    """Matching is by resolved path: the same directory reached two ways is
    one source, not two indexes of the same content."""
    from pheasant.cli import main
    from pheasant.config.loader import load_config

    config_path = tmp_path / "pheasant.yaml"
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "a.md").write_text("# A\n\nSome prose.\n", encoding="utf-8")

    main(["up", str(notes), "-c", str(config_path), "--no-serve"])
    main(["up", str(notes) + "/", "-c", str(config_path), "--no-serve"])

    assert len(load_config(config_path).sources) == 1
