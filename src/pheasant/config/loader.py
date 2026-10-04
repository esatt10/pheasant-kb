from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml

from pheasant.config.profiles import profile_data, profile_names
from pheasant.config.schema import PheasantConfig

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    pass


#: Settings that existed once and no longer do. ``model_validate`` already
#: drops unknown keys, so a config carrying one of these still loads — which
#: is the behaviour we want (``/state`` and the configs beside it are user
#: data, CLAUDE.md rule 2) but is *silent*, and silence here reads as "the
#: setting is still doing something". Each entry says what to do instead.
REMOVED_SETTINGS: dict[str, str] = {
    "obsidian": (
        "the Obsidian vault projection was removed; the UI's graph workspace "
        "(/graph) replaces it. Index a vault as a *source* with "
        "type: markdown_folder."
    ),
    "graph.concept_min_documents": (
        "concept extraction was retired — it was 87% of graph nodes and 98.6% "
        "of edges while failing every retrieval test set for it, so this knob "
        "has had no effect since. Terms are still searchable; the corpus "
        "vocabulary now comes from the full-text index. Delete the key."
    ),
    "pheasant.vault_path": (
        "the Obsidian vault projection was removed, so pheasant no longer "
        "writes a vault. Delete the key, and drop any /vault mount. Files "
        "already under it are yours and are left untouched — index them by "
        "adding them as an ordinary source if you still want them."
    ),
}

#: Warn once per process per key. Config is re-read on a number of paths
#: (every `/config` request, each sync worker start), and a deprecation notice
#: that repeats a few hundred times a day trains people to filter it out.
_WARNED: set[str] = set()


def warn_on_removed_settings(data: dict[str, Any]) -> list[str]:
    """Log a one-line notice for each removed setting still present.

    Returns the keys found, so callers (and tests) can assert on them rather
    than scraping log output.
    """

    found: list[str] = []
    for key, guidance in REMOVED_SETTINGS.items():
        section, _, leaf = key.partition(".")
        if leaf:
            block = data.get(section)
            present = isinstance(block, dict) and leaf in block
        else:
            present = section in data
        if not present:
            continue
        found.append(key)
        if key not in _WARNED:
            _WARNED.add(key)
            logger.warning("Ignoring removed config setting %r: %s", key, guidance)
    return found


def load_config(path: str | Path) -> PheasantConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file does not exist: {config_path}")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError("Config root must be a mapping")
    warn_on_removed_settings(data)
    return PheasantConfig.model_validate(data)


def load_layered_config(
    path: str | Path | None = None,
    profile: str | None = "quickstart",
    overrides: dict[str, Any] | None = None,
) -> PheasantConfig:
    data = PheasantConfig().model_dump(mode="json")
    data = deep_merge(data, profile_data(profile))
    if path is not None and Path(path).exists():
        loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError("Config root must be a mapping")
        # Check the user's file, not the merged result: the defaults this
        # started from no longer contain the removed keys at all.
        warn_on_removed_settings(loaded)
        data = deep_merge(data, loaded)
    data = deep_merge(data, overrides or {})
    return PheasantConfig.model_validate(data)


def effective_config_dict(
    path: str | Path | None = None,
    profile: str | None = "quickstart",
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return load_layered_config(path, profile, overrides).model_dump(mode="json")


def render_init_config(profile: str = "quickstart") -> str:
    data = deep_merge(PheasantConfig().model_dump(mode="json"), profile_data(profile))
    data["sources"] = []
    header = (
        f"# pheasant {profile} profile configuration\n"
        f"# Available profiles: {', '.join(profile_names())}\n"
        "# Add sources under the sources: list when ready.\n"
    )
    rendered = dump_config_yaml(data)
    rendered = rendered.replace("sources:\n\n", "sources: []\n")
    return header + rendered


def dump_config_yaml(data: dict[str, Any]) -> str:
    """Dump a config mapping as YAML people will actually read and edit.

    One deviation from ``yaml.safe_dump``: block sequences are indented under
    their key rather than sitting at the parent's indent, so a generated
    ``pheasant.yaml`` looks like the hand-written one in
    ``pheasant.example.yaml``. Every writer of config YAML goes through here,
    so that shape is decided once.

    Key order is preserved (``sort_keys=False``) because these files are
    generated to be read: a config whose sections come out alphabetised is
    harder to scan than one that follows the schema's own order.
    """

    class _IndentingDumper(yaml.SafeDumper):
        def increase_indent(self, flow=False, indentless=False):  # noqa: ARG002
            # `indentless=True` is what un-indents block sequences.
            return super().increase_indent(flow, False)

    return yaml.dump(data, Dumper=_IndentingDumper, sort_keys=False, default_flow_style=False)


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def parse_override_pairs(pairs: list[str] | None) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ConfigError(f"Override must use key=value syntax: {pair}")
        key, raw_value = pair.split("=", 1)
        cursor = data
        parts = key.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = _parse_override_value(raw_value)
    return data


def _parse_override_value(raw_value: str) -> Any:
    lowered = raw_value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None
    try:
        return int(raw_value)
    except ValueError:
        pass
    try:
        return float(raw_value)
    except ValueError:
        pass
    return raw_value


def config_hash(config: PheasantConfig | dict[str, Any]) -> str:
    payload = config.model_dump(mode="json") if isinstance(config, PheasantConfig) else config
    text = yaml.safe_dump(payload, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_source_paths(config: PheasantConfig, require_exists: bool = False) -> list[str]:
    errors: list[str] = []
    for source in config.sources:
        if require_exists and not source.path.exists():
            errors.append(f"source {source.name} path does not exist: {source.path}")
    return errors
