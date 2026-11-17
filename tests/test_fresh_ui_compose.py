"""Contract tests for the one-command, UI-native Docker reset path."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "deploy" / "compose" / "docker-compose.fresh.yml"
ENTRYPOINT_PATH = ROOT / "docker-entrypoint.sh"


def test_fresh_ui_compose_is_standalone_and_volume_backed() -> None:
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    service = raw["services"]["pheasant"]

    assert service["build"] == {"context": "../.."}
    assert service["container_name"] == "${PHEASANT_CONTAINER_NAME:-pheasant}"
    assert service["ports"] == ["${PHEASANT_BIND:-127.0.0.1}:${PHEASANT_PORT:-8765}:8765"]
    assert service["environment"]["PHEASANT_CONFIG"] == "/config/pheasant.yaml"
    assert service["environment"]["PHEASANT_WORKSPACE"] == "/ui-managed-sources"
    assert service["volumes"] == [
        "pheasant-config:/config",
        "pheasant-state:/state",
        "pheasant-exports:/exports",
    ]
    assert set(raw["volumes"]) == {
        "pheasant-config",
        "pheasant-state",
        "pheasant-exports",
    }


def test_fresh_ui_compose_resets_only_its_mounted_pheasant_volumes() -> None:
    raw = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    service = raw["services"]["pheasant"]
    script = (ROOT / "docker-fresh-entrypoint.sh").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert service["entrypoint"] == ["/app/docker-fresh-entrypoint.sh"]
    assert "COPY docker-fresh-entrypoint.sh /app/docker-fresh-entrypoint.sh" in dockerfile
    assert "find /config -mindepth 1 -delete" in script
    assert "find /state -mindepth 1 -delete" in script
    assert "find /exports -mindepth 1 -delete" in script
    assert "exec /app/docker-entrypoint.sh serve" in script
    assert "/workspace" not in script
    assert "docker-compose.override.yml" not in script


def _entrypoint_wasm_answers() -> dict:
    """The answers JSON the container feeds `pheasant setup`, parsed."""
    script = ENTRYPOINT_PATH.read_text(encoding="utf-8")
    match = re.search(r"^WASM_ANSWERS='(.*)'$", script, re.M)
    assert match, "docker-entrypoint.sh no longer defines WASM_ANSWERS"
    return json.loads(match.group(1))


def test_a_fresh_container_generates_a_config_with_wasm_acceleration_on() -> None:
    """The image always installs the [wasm] extra, so a container that writes
    its own config should use what it was built with.

    Driven through the **real wizard** rather than asserting on the script's
    text: the two flags are schema keys, and a rename would leave a
    string-matching test green while the container silently generated a config
    with acceleration off — which is exactly the failure mode, since both
    accelerators fall back to pure Python without ever erroring.
    """
    from pheasant.setup_wizard import Wizard

    wizard = Wizard(accept_defaults=True, preset=_entrypoint_wasm_answers())
    wizard.run()
    rendered = wizard.config_dict()

    assert rendered["search"]["wasm_relationship_search"] is True
    assert rendered["graph"]["wasm_cross_source_resolution"] is True


def test_the_default_config_without_those_answers_still_has_wasm_off() -> None:
    """The flags are opt-in in the schema and stay that way — the container
    turning them on is a container decision, not a changed default."""
    from pheasant.setup_wizard import Wizard

    wizard = Wizard(accept_defaults=True)
    wizard.run()
    rendered = wizard.config_dict()

    assert rendered.get("search", {}).get("wasm_relationship_search", False) is False
    assert rendered.get("graph", {}).get("wasm_cross_source_resolution", False) is False


def test_the_entrypoint_gates_wasm_on_the_extra_being_installed() -> None:
    """Enabling it without wasmtime would still be *correct* — both accelerators
    fall back — but it would log a warning per call and mean nothing."""
    script = ENTRYPOINT_PATH.read_text(encoding="utf-8")
    assert 'python -c "import wasmtime"' in script
    assert "--answers" in script


def test_the_entrypoint_preserves_roles_and_dispatches_grpc_workers() -> None:
    script = ENTRYPOINT_PATH.read_text(encoding="utf-8")

    assert 'exec python -m pheasant serve --config "$CONFIG_PATH" "$@"' in script
    assert "restore|worker|" in script
    assert 'if [ "$serve_role" = all ]; then' in script


def test_the_sandboxed_pdf_extractor_is_not_swept_in_with_the_accelerators() -> None:
    """It is a fidelity trade, not an acceleration one.

    The default (`auto`, which prefers PDFium/python-docx) reads encrypted
    PDFs, LZW/CCITT and Type0/CID CMaps that the sandboxed tokenizer does not,
    so a fresh container must not silently downgrade extraction while claiming
    only to have turned on acceleration.
    """
    answers = _entrypoint_wasm_answers()

    assert set(answers) == {
        "search.wasm_relationship_search",
        "graph.wasm_cross_source_resolution",
    }
    assert all(value is True for value in answers.values())


def test_fresh_ui_one_liner_is_documented() -> None:
    command = (
        "docker compose -f deploy/compose/docker-compose.fresh.yml up -d --build --force-recreate"
    )
    # Documented where someone looks for it — the how-to guide — and in the
    # compose file's own header. The README is the product front door and
    # deliberately does not carry every operational one-liner.
    assert command in COMPOSE_PATH.read_text(encoding="utf-8")
    assert command in (ROOT / "docs/how-to/run-the-ui.md").read_text(encoding="utf-8")
