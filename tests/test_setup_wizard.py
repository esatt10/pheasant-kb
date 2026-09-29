"""``pheasant setup`` — the interactive configuration wizard.

Acceptance:

1. Pressing Enter through the whole interview produces a config the loader
   accepts — every default is a working answer.
2. Answers are written where they say they are: dotted keys land in the right
   nested config block, and sources resolve through the same target detection
   ``pheasant up`` uses.
3. Secrets never reach the YAML. They go to a ``.env`` with mode 0600, an
   existing ``.env`` is merged rather than clobbered, and the file is
   gitignored.
4. Defaults are read off the live schema, not copied — the property that
   replaced the CI freshness test on the old prose wizard.
5. An interrupted run is resumable and does not checkpoint secrets.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import yaml

from pheasant.cli import main
from pheasant.config.loader import load_config
from pheasant.config.schema import PheasantConfig
from pheasant.setup_wizard import (
    PROGRESS_FILENAME,
    Prompter,
    ScriptedPrompter,
    Wizard,
    build_phases,
    build_sections,
    ensure_gitignored,
    load_progress,
    merge_env_file,
    render_config_yaml,
    save_progress,
    schema_default,
    startup_commands,
    write_env_file,
)

SAMPLE_WORKSPACE = Path(__file__).resolve().parent / "fixtures" / "sample_workspace"


# ------------------------------------------------------------------ defaults


def test_defaults_are_read_off_the_live_schema() -> None:
    assert schema_default("server.port") == PheasantConfig().server.port
    assert schema_default("search.embeddings.model") == PheasantConfig().search.embeddings.model
    # Paths come back as strings so they render into YAML as scalars.
    assert schema_default("pheasant.state_path") == str(PheasantConfig().pheasant.state_path)


def test_every_question_points_at_a_real_schema_field() -> None:
    """A typo in a dotted key is caught here, not by a user mid-interview."""
    for section in build_sections():
        for question in section.questions:
            if question.key.startswith("__"):
                continue
            schema_default(question.key)  # raises AttributeError on a bad path


def test_pressing_enter_through_everything_yields_a_loadable_config(tmp_path: Path) -> None:
    wizard = Wizard(prompter=ScriptedPrompter([]))
    wizard.run()
    data = wizard.config_dict()
    path = tmp_path / "pheasant.yaml"
    path.write_text(render_config_yaml(data), encoding="utf-8")
    config = load_config(path)
    assert config.pheasant.name == PheasantConfig().pheasant.name
    assert config.server.port == PheasantConfig().server.port


# ------------------------------------------------------------------ answers


def test_answers_land_in_the_right_config_block() -> None:
    wizard = Wizard(
        accept_defaults=True,
        preset={
            "pheasant.name": "kb-one",
            "server.port": 9100,
            "search.embeddings.enabled": True,
            "search.embeddings.provider": "stub",
            "assistant.retrieval.max_rounds": 4,
        },
    )
    wizard.run()
    data = wizard.config_dict()
    assert data["pheasant"]["name"] == "kb-one"
    assert data["server"]["port"] == 9100
    assert data["search"]["embeddings"]["provider"] == "stub"
    assert data["assistant"]["retrieval"]["max_rounds"] == 4
    config = PheasantConfig.model_validate(data)
    assert config.assistant.retrieval.max_rounds == 4


def test_choice_questions_reject_a_value_that_is_not_offered() -> None:
    prompter = ScriptedPrompter(["sideways", "text"])
    wizard = Wizard(prompter=prompter, preset={})
    section = next(s for s in build_sections() if s.id == "search")
    question = next(q for q in section.questions if q.key == "search.default_mode")
    wizard._ask(question)
    assert wizard.answers["search.default_mode"] == "text"
    assert any("pick one of" in line for line in prompter.transcript)


def test_assistant_model_is_explicitly_asked_when_chat_is_enabled() -> None:
    wizard = Wizard(
        prompter=ScriptedPrompter(["gpt-test"]),
        preset={"assistant.enabled": True, "assistant.provider": "openai"},
    )
    question = next(q for s in build_sections() for q in s.questions if q.key == "assistant.model")
    wizard._ask(question)
    assert wizard.answers["assistant.model"] == "gpt-test"


def test_setup_uses_six_responsive_phases() -> None:
    assert [phase.title for phase in build_phases()] == [
        "Foundation",
        "Sources & content",
        "Search & graph",
        "Assistant & memory",
        "Operations & security",
        "Federation",
    ]


def test_explicit_provider_writes_recommended_model_and_custom_model() -> None:
    question = next(q for s in build_sections() for q in s.questions if q.key == "assistant.model")
    recommended = Wizard(
        prompter=ScriptedPrompter([]),
        preset={"assistant.enabled": True, "assistant.provider": "openai"},
        accept_defaults=True,
    )
    recommended._ask(question)
    assert recommended.answers["assistant.model"] == "gpt-6-luna"

    custom = Wizard(
        prompter=ScriptedPrompter(["2", "my-account-model"]),
        preset={"assistant.enabled": True, "assistant.provider": "openai"},
    )
    custom._ask(question)
    assert custom.answers["assistant.model"] == "my-account-model"


def test_auto_model_is_resolved_for_display_but_never_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "present")
    question = next(q for s in build_sections() for q in s.questions if q.key == "assistant.model")
    wizard = Wizard(
        prompter=ScriptedPrompter([]),
        preset={
            "assistant.enabled": True,
            "assistant.provider": "auto",
            "assistant.model": "unsafe",
        },
    )
    wizard._ask(question)
    assert wizard.answers["assistant.model"] is None
    assert any("OpenAI" in line for line in wizard.prompter.transcript)


def test_plain_output_is_wrapped_and_has_no_ansi(capsys: pytest.CaptureFixture[str]) -> None:
    prompter = Prompter(plain=True, width=40, interactive=False)
    Wizard(prompter=prompter, accept_defaults=True).run()
    output = capsys.readouterr().out
    assert all(len(line) <= 40 for line in output.splitlines())
    assert "\033[" not in output


def test_embedding_env_question_rejects_a_pasted_secret() -> None:
    prompter = ScriptedPrompter(["sk-pasted-secret", "OPENAI_API_KEY"])
    wizard = Wizard(
        prompter=prompter,
        preset={
            "search.embeddings.enabled": True,
            "search.embeddings.provider": "openai-spec",
        },
    )
    question = next(
        q for s in build_sections() for q in s.questions if q.key == "search.embeddings.api_key_env"
    )
    wizard._ask(question)
    assert wizard.answers[question.key] == "OPENAI_API_KEY"
    assert any("do not paste the key itself" in line for line in prompter.transcript)


def test_bool_questions_accept_yes_and_no_and_reject_prose() -> None:
    prompter = ScriptedPrompter(["maybe", "no"])
    wizard = Wizard(prompter=prompter)
    # Any plain (non-advanced) bool question will do — an advanced one would
    # short-circuit to its default without ever consulting the prompter.
    question = next(
        q
        for section in build_sections()
        for q in section.questions
        if q.kind == "bool" and not q.advanced and q.when is None
    )
    wizard._ask(question)
    assert wizard.answers[question.key] is False


def test_sources_resolve_through_the_same_detection_as_pheasant_up() -> None:
    wizard = Wizard(prompter=ScriptedPrompter([str(SAMPLE_WORKSPACE), ""]))
    wizard._ask_sources()
    assert len(wizard.sources) == 1
    assert wizard.sources[0]["path"] == str(SAMPLE_WORKSPACE)
    # Round-trips through the loader as a real source, not just a dict.
    config = PheasantConfig.model_validate({"sources": wizard.sources})
    assert config.sources[0].name == wizard.sources[0]["name"]


def test_detected_document_folder_admits_documents() -> None:
    """The setup-generated source must not fall back to code-only includes."""
    wizard = Wizard(prompter=ScriptedPrompter([str(SAMPLE_WORKSPACE / "documents"), ""]))
    wizard._ask_sources()
    assert wizard.sources[0]["type"] == "document_folder"
    assert wizard.sources[0]["include"] == ["**/*"]


def test_a_bad_target_is_reported_and_reasked_rather_than_fatal(tmp_path: Path) -> None:
    prompter = ScriptedPrompter([str(tmp_path / "nope"), str(SAMPLE_WORKSPACE), ""])
    wizard = Wizard(prompter=prompter)
    wizard._ask_sources()
    assert len(wizard.sources) == 1
    assert any(line.startswith("    !") for line in prompter.transcript)


def test_advanced_questions_are_skipped_but_still_take_their_default() -> None:
    plain = Wizard(prompter=ScriptedPrompter([]), advanced=False)
    plain.run()
    # `sync.limits.max_files` is advanced-only; it must still be in the config.
    assert plain.answers["sync.limits.max_files"] == schema_default("sync.limits.max_files")


# ------------------------------------------------------------------ secrets


def test_a_secret_never_reaches_the_config_and_lands_in_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    wizard = Wizard(
        prompter=ScriptedPrompter(["sk-secret-value"]),
        preset={
            "search.embeddings.enabled": True,
            "search.embeddings.provider": "openai-spec",
            "search.embeddings.api_key_env": "OPENAI_API_KEY",
        },
    )
    section = next(s for s in build_sections() if s.id == "embeddings")
    question = next(q for q in section.questions if q.kind == "secret")
    wizard._ask(question)

    assert wizard.env_values() == {"OPENAI_API_KEY": "sk-secret-value"}
    rendered = render_config_yaml(wizard.config_dict())
    assert "sk-secret-value" not in rendered
    assert "OPENAI_API_KEY" in rendered  # the NAME is config; the value is not


def test_secret_prompt_is_skipped_when_the_variable_is_already_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "already-there")
    prompter = ScriptedPrompter(["should-not-be-read"])
    wizard = Wizard(
        prompter=prompter,
        preset={
            "search.embeddings.provider": "openai-spec",
            "search.embeddings.api_key_env": "OPENAI_API_KEY",
        },
    )
    section = next(s for s in build_sections() if s.id == "embeddings")
    wizard._ask(next(q for q in section.questions if q.kind == "secret"))
    assert wizard.env_values() == {}


def test_env_file_is_written_0600(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    write_env_file(env_path, {"OPENAI_API_KEY": "sk-1"})
    mode = stat.S_IMODE(env_path.stat().st_mode)
    assert mode == 0o600, f"secrets file is mode {mode:o}"
    assert env_path.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-1\n"


def test_existing_env_is_merged_never_clobbered(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# my notes\nUNRELATED=keep-me\nOPENAI_API_KEY=old\n",
        encoding="utf-8",
    )
    report = write_env_file(env_path, {"OPENAI_API_KEY": "new", "ANTHROPIC_API_KEY": "a"})
    text = env_path.read_text(encoding="utf-8")
    assert "# my notes" in text
    assert "UNRELATED=keep-me" in text
    assert "OPENAI_API_KEY=new" in text
    assert "OPENAI_API_KEY=old" not in text
    assert report["updated"] == ["OPENAI_API_KEY"]
    assert report["added"] == ["ANTHROPIC_API_KEY"]


def test_env_values_needing_quotes_are_quoted(tmp_path: Path) -> None:
    text, _, _ = merge_env_file(tmp_path / ".env", {"K": "has space#and$dollar"})
    assert text.strip() == 'K="has space#and\\$dollar"'


def test_env_is_added_to_gitignore_inside_a_git_worktree(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    env_path = tmp_path / ".env"
    env_path.write_text("K=v\n", encoding="utf-8")
    note = ensure_gitignored(env_path, repo_root=tmp_path)
    assert note is not None
    assert ".env" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # Idempotent: a second call changes nothing.
    assert ensure_gitignored(env_path, repo_root=tmp_path) is None


def test_gitignore_is_untouched_outside_a_git_worktree(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("K=v\n", encoding="utf-8")
    assert ensure_gitignored(env_path, repo_root=tmp_path) is None
    assert not (tmp_path / ".gitignore").exists()


# ------------------------------------------------------------------ resume


def test_progress_round_trips_and_omits_secrets(tmp_path: Path) -> None:
    wizard = Wizard(preset={"pheasant.name": "half-done"})
    wizard.secrets["OPENAI_API_KEY"] = "sk-never-checkpoint-me"
    wizard.sources = [{"name": "a", "type": "document_folder", "path": "/x"}]
    path = tmp_path / PROGRESS_FILENAME
    save_progress(path, wizard)

    assert "sk-never-checkpoint-me" not in path.read_text(encoding="utf-8")
    answers, sources = load_progress(path)
    assert answers["pheasant.name"] == "half-done"
    assert sources[0]["name"] == "a"


def test_a_corrupt_progress_file_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    path = tmp_path / PROGRESS_FILENAME
    path.write_text("{not json", encoding="utf-8")
    assert load_progress(path) == ({}, [])


# ------------------------------------------------------------------ the CLI


def test_cli_setup_accept_defaults_writes_a_loadable_config(tmp_path: Path) -> None:
    output = tmp_path / "pheasant.yaml"
    assert main(["setup", "--accept-defaults", "-o", str(output)]) == 0
    assert load_config(output).server.port == 8765


def test_cli_setup_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    output = tmp_path / "pheasant.yaml"
    output.write_text("pheasant:\n  name: mine\n", encoding="utf-8")
    assert main(["setup", "--accept-defaults", "-o", str(output)]) == 1
    assert "mine" in output.read_text(encoding="utf-8")
    assert main(["setup", "--accept-defaults", "-o", str(output), "--force"]) == 0


def test_cli_setup_answers_file_drives_the_interview(tmp_path: Path) -> None:
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps({"pheasant.name": "from-file", "server.port": 9999}),
        encoding="utf-8",
    )
    output = tmp_path / "pheasant.yaml"
    assert (
        main(
            [
                "setup",
                "--accept-defaults",
                "--answers",
                str(answers),
                "-o",
                str(output),
            ]
        )
        == 0
    )
    config = load_config(output)
    assert config.pheasant.name == "from-file"
    assert config.server.port == 9999


def test_cli_setup_clears_its_progress_file_on_success(tmp_path: Path) -> None:
    progress = tmp_path / PROGRESS_FILENAME
    progress.write_text(json.dumps({"answers": {"pheasant.name": "resumed"}}), encoding="utf-8")
    output = tmp_path / "pheasant.yaml"
    assert main(["setup", "--accept-defaults", "-o", str(output)]) == 0
    assert load_config(output).pheasant.name == "resumed"
    assert not progress.exists()


def test_startup_commands_cover_every_deployment_target(tmp_path: Path) -> None:
    config = tmp_path / "pheasant.yaml"
    local = "\n".join(startup_commands(config, "local", 8765))
    docker = "\n".join(startup_commands(config, "docker", 9000))
    compose = "\n".join(startup_commands(config, "compose", 8765))
    assert "pheasant start" in local
    assert "9000:8765" in docker
    assert "docker compose up" in compose


def test_generated_yaml_round_trips(tmp_path: Path) -> None:
    """A wizard-written config must reload with its lists intact."""
    wizard = Wizard(accept_defaults=True)
    wizard.run()
    wizard.sources = [
        {
            "name": "notes",
            "type": "document_folder",
            "path": str(SAMPLE_WORKSPACE),
            "description": "d",
            "include": ["**/*.md"],
        }
    ]
    text = render_config_yaml(wizard.config_dict())
    parsed = yaml.safe_load(text)
    assert parsed["sources"][0]["include"] == ["**/*.md"]
    assert parsed["sources"][0]["name"] == "notes"


def test_setup_does_not_write_an_env_file_when_no_secrets_were_given(tmp_path: Path) -> None:
    output = tmp_path / "pheasant.yaml"
    env = tmp_path / ".env"
    assert main(["setup", "--accept-defaults", "-o", str(output), "--env-output", str(env)]) == 0
    assert not env.exists()


def test_env_helper_never_leaves_a_world_readable_window(tmp_path: Path) -> None:
    """The mode is set on creation, not after the secret is written."""
    env_path = tmp_path / "nested" / ".env"
    write_env_file(env_path, {"K": "v"})
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert os.access(env_path, os.R_OK)
