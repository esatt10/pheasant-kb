"""Acceptance tests for configuration loading and validation."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path

from pheasant.config.schema import ModelMixin
from tests.conftest import call_cli, first_attr, import_any, require_attr


def _as_mapping_or_attrs(config: object) -> object:
    return config


def _get(config: object, key: str) -> object:
    if isinstance(config, Mapping):
        return config[key]
    return getattr(config, key)


def _source_names(config: object) -> set[str]:
    sources = _get(_as_mapping_or_attrs(config), "sources")
    names: set[str] = set()
    for source in sources:  # type: ignore[union-attr]
        names.add(str(source["name"] if isinstance(source, Mapping) else source.name))
    return names


def test_config_loads_example_yaml_with_expected_sources(
    loaded_config: object, state_path: Path
) -> None:
    """The rendered example config should load and preserve configured paths/sources."""

    assert _source_names(loaded_config) == {
        "pheasant-repo",
        "architecture-notes",
        "product-documents",
    }

    settings = (
        _get(loaded_config, "pheasant") if isinstance(loaded_config, Mapping) else loaded_config
    )
    state_value = str(_get(settings, "state_path"))
    assert str(state_path) in state_value


def test_config_validation_reports_missing_source_path(tmp_path: Path, config_path: Path) -> None:
    """Invalid configured paths should be rejected or reported clearly."""

    bad_config = tmp_path / "missing-source.yaml"
    bad_config.write_text(
        config_path.read_text(encoding="utf-8").replace("/pheasant-repo", "/missing-repo"),
        encoding="utf-8",
    )

    config_module = import_any(("pheasant.config.loader", "pheasant.config"))
    loader = require_attr(
        config_module, ("load_config", "load_pheasant_config", "load", "from_yaml"), "config loader"
    )
    try:
        loaded = loader(bad_config)
    except Exception as exc:  # noqa: BLE001 - acceptance test checks user-facing validation text.
        message = str(exc).lower()
        assert "missing-repo" in message or "does not exist" in message or "not found" in message
        return

    validator = first_attr(
        config_module, ("validate_config", "validate", "validate_paths", "validate_source_paths")
    ) or first_attr(loaded, ("validate", "validate_paths"))
    assert validator is not None, (
        "Config loader must validate paths or expose an explicit validator."
    )
    try:
        validation_result = validator(loaded, require_exists=True)
    except TypeError:
        try:
            validation_result = validator(loaded)
        except TypeError:
            validation_result = validator()
    except Exception as exc:  # noqa: BLE001 - acceptance test checks user-facing validation text.
        message = str(exc).lower()
        assert "missing-repo" in message or "does not exist" in message or "not found" in message
        return

    if validation_result:
        message = str(validation_result).lower()
        assert "missing-repo" in message or "does not exist" in message or "not found" in message
        return
    raise AssertionError("Config validation accepted a source path that does not exist.")


def test_cli_validate_accepts_rendered_config(config_path: Path) -> None:
    """The user-facing `pheasant validate` command should accept the example config."""

    result = call_cli(["validate", str(config_path)])
    assert result.returncode == 0, result.stderr or result.stdout
    assert "error" not in result.stderr.lower()


def test_graph_section_of_the_yaml_is_actually_applied() -> None:
    """`PheasantConfig.model_validate` must respect a `graph:` YAML section.

    Regression test: `graph=build(GraphSettings, data.get("graph"))` was
    missing from `model_validate`'s constructor call, so ANY `graph:`
    section in a config file — `memory_entity_bridging`,
    `wasm_cross_source_resolution` — was silently discarded in favor of
    `GraphSettings()` defaults, with no error and no test catching it.
    Found live: `docker-compose`-deployed config showed
    `wasm_cross_source_resolution: false` in the resolved `/config`
    response despite the mounted YAML setting it `true`.
    """
    from pheasant.config.schema import PheasantConfig

    config = PheasantConfig.model_validate(
        {
            "pheasant": {"name": "graph-section-regression"},
            "graph": {
                "memory_entity_bridging": False,
                "wasm_cross_source_resolution": True,
            },
        }
    )
    assert config.graph.memory_entity_bridging is False
    assert config.graph.wasm_cross_source_resolution is True


def test_example_config_has_no_duplicate_top_level_keys() -> None:
    """`pheasant.example.yaml` must not define the same top-level key twice.

    Regression test for a real defect in the shipped reference config: it had
    **two** `sync:` blocks. YAML keeps the last occurrence, so the entire
    documented `sync.limits` guardrail block — the one whose own comments
    describe stopping "I accidentally indexed my home directory" — was
    silently discarded, with no error from any parser and no test catching it.
    A duplicate key here is always a bug: it means a section the file appears
    to document has no effect.
    """
    import re

    example = Path(__file__).resolve().parents[1] / "pheasant.example.yaml"
    keys = re.findall(r"^([a-z_]+):", example.read_text(encoding="utf-8"), flags=re.M)
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    assert not duplicates, f"duplicate top-level keys in pheasant.example.yaml: {duplicates}"


def test_example_config_still_declares_the_sync_guardrails() -> None:
    """The guardrails must survive as *loaded* config, not just as text."""
    import yaml

    example = Path(__file__).resolve().parents[1] / "pheasant.example.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    limits = (data.get("sync") or {}).get("limits") or {}
    assert limits.get("max_files")
    assert limits.get("max_file_size_mb") == 1024
    assert limits.get("follow_symlinks") is False


def test_example_config_declares_the_document_extractor() -> None:
    import yaml

    example = Path(__file__).resolve().parents[1] / "pheasant.example.yaml"
    data = yaml.safe_load(example.read_text(encoding="utf-8"))
    extractor = (data.get("ingestion") or {}).get("extractor") or {}
    assert extractor.get("provider") == "auto"
    assert extractor.get("html_text") is False


def test_config_with_removed_obsidian_settings_still_loads(tmp_path: Path) -> None:
    """A pre-removal config must keep loading, not hard-fail.

    The Obsidian projection went away, but the YAML files describing it are
    user data sitting on real disks. `model_validate` drops unknown keys on
    its own, so what this pins is that the removal is *reported* rather than
    silently ignored.
    """

    from pheasant.config.loader import (
        REMOVED_SETTINGS,
        load_config,
        load_layered_config,
        warn_on_removed_settings,
    )

    config_path = tmp_path / "legacy.yaml"
    config_path.write_text(
        "pheasant:\n"
        "  name: legacy-kb\n"
        f"  state_path: {tmp_path / 'state'}\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  exports_path: {tmp_path / 'exports'}\n"
        f"  workspace_root: {tmp_path}\n"
        "obsidian:\n"
        "  enabled: true\n"
        "  template_profile: engineering\n"
        "sources: []\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    assert config.pheasant.name == "legacy-kb"
    assert not hasattr(config, "obsidian")
    assert not hasattr(config.pheasant, "vault_path")

    # Both loaders report both removed keys, and every key carries guidance.
    raw = {"pheasant": {"vault_path": "/vault"}, "obsidian": {"enabled": True}}
    assert sorted(warn_on_removed_settings(raw)) == ["obsidian", "pheasant.vault_path"]
    assert all(REMOVED_SETTINGS[key] for key in REMOVED_SETTINGS)

    layered = load_layered_config(config_path, profile="dev", overrides={})
    assert layered.pheasant.name == "legacy-kb"


def test_removed_setting_warning_is_logged_once_per_process(tmp_path: Path, caplog: object) -> None:
    """Config is re-read constantly; a notice that repeats gets filtered out."""

    import logging

    from pheasant.config import loader as loader_module

    loader_module._WARNED.discard("obsidian")
    raw = {"obsidian": {"enabled": True}}
    with caplog.at_level(logging.WARNING, logger="pheasant.config.loader"):  # type: ignore[attr-defined]
        loader_module.warn_on_removed_settings(raw)
        loader_module.warn_on_removed_settings(raw)

    obsidian_warnings = [r for r in caplog.records if "obsidian" in r.getMessage()]  # type: ignore[attr-defined]
    assert len(obsidian_warnings) == 1
    assert "graph workspace" in obsidian_warnings[0].getMessage()


#: Keys in ``pheasant.example.yaml`` that no schema field backs, with the
#: reason each is deliberate. Every entry here is a **decision**, which is the
#: point of listing them rather than letting the check ignore unknown keys
#: wholesale — the same idiom ``WIZARD_EXEMPT`` and ``NOT_MIGRATED`` use.
#:
#: These four are descriptive: they document behaviour that is not configurable
#: (the lexical engine *is* FTS5; idempotency is a product guarantee, not a
#: setting). They predate this check. Adding a fifth without a reason is what
#: it exists to stop.
EXAMPLE_ONLY_KEYS = {
    "search.keyword": "documents that the lexical engine is FTS5; not selectable",
    "sync.startup": "descriptive; the real knobs are sources[].sync",
    "sync.git.reindex_on_branch_switch": "descriptive; branch handling is not optional",
    "sync.idempotency": "documents pillar 1, which is a guarantee rather than a setting",
}


def test_the_example_config_has_no_keys_the_schema_silently_drops() -> None:
    """A documented key that does nothing is worse than an undocumented one.

    ``model_validate`` filters unknown keys, so a stale block in the reference
    config is invisible: it loads, it validates, and every value in it is
    ignored. ``search.ranking`` sat that way for three releases — four
    plausible-looking keys (``prefer_exact_path_matches``,
    ``prefer_recent_commits``, ``graph_neighbor_boost``,
    ``max_results_default``) that no field backed, in the block a reader would
    copy first when they wanted to change ranking.

    It was harmless while nothing read ``search.ranking``. It stopped being
    harmless the moment that block became the real tuning surface, and the
    person it would have cost is one who copied the reference config and then
    could not work out why their ranking never changed.
    """

    import dataclasses

    import yaml

    from pheasant.config.schema import PheasantConfig

    example = Path(__file__).resolve().parents[1] / "pheasant.example.yaml"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    unknown: list[str] = []

    def walk(dc: type, data: object, path: str) -> None:
        if not dataclasses.is_dataclass(dc) or not isinstance(data, dict):
            return
        fields = {f.name: f for f in dataclasses.fields(dc)}
        for key, value in data.items():
            where = f"{path}.{key}"
            if key not in fields:
                if where not in EXAMPLE_ONLY_KEYS:
                    unknown.append(where)
                continue
            factory = fields[key].default_factory
            if factory is dataclasses.MISSING or not isinstance(value, dict):
                continue
            try:
                nested = factory()
            except Exception:  # noqa: BLE001 - a factory that needs arguments
                continue
            if dataclasses.is_dataclass(nested):
                walk(type(nested), value, where)

    for field in dataclasses.fields(PheasantConfig):
        if field.name == "sources" or field.name not in raw:
            continue
        factory = field.default_factory
        if factory is dataclasses.MISSING:
            continue
        default = factory()
        if dataclasses.is_dataclass(default):
            walk(type(default), raw[field.name], field.name)

    assert not unknown, (
        "pheasant.example.yaml documents keys no schema field backs, so they load "
        f"and are silently ignored: {', '.join(sorted(unknown))}. Add the field, fix "
        "the example, or record the key in EXAMPLE_ONLY_KEYS with its reason."
    )


def test_the_examples_ranking_block_is_the_real_tuning_surface() -> None:
    """Every tunable parameter appears in the reference config, at its default.

    Stronger than "no unknown keys": the block a reader copies has to be
    *complete*, because these are the parameters `pheasant tune` proposes and a
    half-documented surface sends people to read the source.
    """

    import yaml

    from pheasant.search.ranking import DEFAULT_RANKING, PARAMETER_STAGES

    example = Path(__file__).resolve().parents[1] / "pheasant.example.yaml"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    documented = raw.get("search", {}).get("ranking", {})

    missing = sorted(set(PARAMETER_STAGES) - set(documented))
    assert not missing, f"tunable parameters missing from pheasant.example.yaml: {missing}"

    defaults = DEFAULT_RANKING.values()
    drifted = {
        name: (documented[name], defaults[name])
        for name in PARAMETER_STAGES
        if float(documented[name]) != defaults[name]
    }
    assert not drifted, (
        "pheasant.example.yaml states values that are not the defaults, so copying it "
        f"silently changes ranking: {drifted}"
    )


# ---------------------------------------------------------------------------
# Nesting is derived, not wired
#
# `model_validate` used to carry ~90 lines of `if dc is ServerSettings: if
# "mcp" in raw: raw["mcp"] = build(McpSettings, ...)` — one branch per nested
# section. Miss a line and the section loaded silently as defaults: no error,
# no warning, and a config file whose every value was ignored.
#
# That edit was the one rule 11's freshness test did not cover, which made it
# the one that could be forgotten.
# ---------------------------------------------------------------------------


def test_every_nested_section_in_the_schema_actually_loads() -> None:
    """Derived from the dataclasses, so a new section needs no second edit.

    Walks the whole tree, sets one non-default value on every nested section it
    finds, and asserts the value survives the round trip. A section that
    `model_validate` failed to construct would come back as its default —
    which is exactly the silent failure this replaced.
    """

    import dataclasses
    import typing
    from typing import get_type_hints

    from pheasant.config.schema import ModelMixin, PheasantConfig

    def probe(dc: type, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], object]]:
        """(dotted path, a value that is not the default) per nested section."""

        found: list[tuple[tuple[str, ...], object]] = []
        hints = get_type_hints(dc)
        for field in dataclasses.fields(dc):
            annotation = hints[field.name]
            # A *container* of sections is not a section: `list[SourceConfig]`
            # unwraps to `(SourceConfig,)` too, and walking into it asks for a
            # dataclass with three required arguments.
            if typing.get_origin(annotation) in {list, tuple, dict, set}:
                continue
            nested = next(
                (
                    candidate
                    for candidate in (getattr(annotation, "__args__", None) or (annotation,))
                    if isinstance(candidate, type) and issubclass(candidate, ModelMixin)
                ),
                None,
            )
            if nested is None or nested is dc:
                continue
            # A bool field is the cheapest probe: flip the default.
            flag = next(
                (
                    inner
                    for inner in dataclasses.fields(nested)
                    if get_type_hints(nested).get(inner.name) is bool
                ),
                None,
            )
            if flag is not None:
                default = getattr(nested(), flag.name)
                found.append(((*path, field.name, flag.name), not default))
            found.extend(probe(nested, (*path, field.name)))
        return found

    probes = probe(PheasantConfig)
    assert len(probes) >= 15, f"only found {len(probes)} nested sections to probe"

    for dotted, value in probes:
        payload: dict = {}
        cursor = payload
        for key in dotted[:-1]:
            cursor = cursor.setdefault(key, {})
        cursor[dotted[-1]] = value

        config = PheasantConfig.model_validate(payload)
        loaded: object = config
        for key in dotted:
            loaded = getattr(loaded, key)
        assert loaded == value, (
            f"{'.'.join(dotted)} did not survive model_validate: got {loaded!r}, "
            f"expected {value!r}. A nested section that is not constructed loads as "
            "its defaults, silently."
        )


@dataclasses.dataclass
class _Inner(ModelMixin):
    """A section this file invents. Module level, because that is where every
    real one lives and where `get_type_hints` can resolve it from."""

    enabled: bool = False
    threshold: int = 1
    where: Path | None = None


@dataclasses.dataclass
class _Outer(ModelMixin):
    label: str = "unset"
    inner: _Inner = dataclasses.field(default_factory=_Inner)


def test_a_new_nested_section_needs_no_wiring() -> None:
    """The property, stated directly rather than inferred from the sweep above.

    A two-level config this file invents, built by the same `_build` every real
    section goes through. Nothing in `model_validate` knows these names — and
    that is the whole change: nesting is read off the annotations, so adding a
    section is one edit to `schema.py` rather than two, the second of which was
    invisible when forgotten.
    """

    from pheasant.config.schema import _build

    built = _build(
        _Outer,
        {"label": "set", "inner": {"enabled": True, "threshold": "7", "where": "/state/x"}},
    )

    assert isinstance(built, _Outer)
    assert built.label == "set"
    assert isinstance(built.inner, _Inner)
    assert built.inner.enabled is True
    # Scalar coercion still runs at every level: "7" from YAML is an int here.
    assert built.inner.threshold == 7
    # …and so does Path coercion, derived from the annotation rather than from
    # a list of field names somebody kept up to date.
    assert built.inner.where == Path("/state/x")

    # An absent section is its own default, not None: this is what makes every
    # field's default the single source of truth the wizard reads.
    assert _build(_Outer, {}).inner == _Inner()
    assert _build(_Outer, None).inner == _Inner()


def test_an_unknown_key_is_ignored_rather_than_fatal() -> None:
    """A config written for a newer version must still load.

    The hand-written builder filtered to known fields at every level; the
    derived one has to keep doing it, or a rolled-back deployment fails to
    start on a config file it wrote itself.
    """

    from pheasant.config.schema import PheasantConfig

    config = PheasantConfig.model_validate(
        {
            "pheasant": {"name": "kb", "invented_by_a_future_version": True},
            "server": {"api": {"enabled": False, "not_a_field": 3}},
        }
    )
    assert config.pheasant.name == "kb"
    assert config.server.api.enabled is False
