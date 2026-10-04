"""First-run bootstrap for ``pheasant up`` (Product Framework Step 30.1).

One command takes a directory from nothing to an indexed, queryable
knowledge base: detect what the directory is, generate a laptop-shaped
config if none exists, then hand off to the ordinary sync engine and
server. Everything here is config plumbing — indexing and serving reuse
the exact code paths behind ``pheasant sync`` and ``pheasant start``.

Generated configs anchor all state under ``.pheasant/{state,exports}``
next to the config file (absolute paths, so later invocations from another
working directory keep hitting the same state). An existing config file is
never rewritten — re-running ``up`` reuses it unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from pheasant.config.loader import deep_merge, dump_config_yaml
from pheasant.config.profiles import profile_data
from pheasant.config.schema import PheasantConfig, SourceType

STATE_DIRNAME = ".pheasant"


def _scrub(obj: Any) -> Any:
    """Drop ``None`` values and empty containers from a config dict.

    ``model_validate`` refills every dropped key with its default, so a
    generated config carries only what someone might want to change rather
    than a wall of ``null``s and empty blocks.
    """
    if isinstance(obj, dict):
        cleaned = {k: _scrub(v) for k, v in obj.items() if v is not None}
        return {k: v for k, v in cleaned.items() if v not in ({}, [])}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj if v is not None]
    return obj


def detect_source_type(path: Path) -> SourceType:
    """Classify a local directory for the generated source entry."""
    if (path / ".obsidian").is_dir():
        return SourceType.markdown_folder
    if (path / ".git").is_dir():
        return SourceType.repository
    return SourceType.document_folder


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workspace"


def state_root(config_path: Path) -> Path:
    """Where a generated config anchors its state/exports/clones."""
    return (config_path.resolve().parent / STATE_DIRNAME).resolve()


def _common_workspace(paths: list[Path], fallback: Path) -> Path:
    """Deepest directory containing every local target.

    ``workspace_root`` anchors relative source paths, so it has to be an
    ancestor of all of them; with nothing local (a pure web/connector
    config) it falls back to the config's own directory.
    """
    directories = [p if p.is_dir() else p.parent for p in paths]
    if not directories:
        return fallback
    if len(directories) == 1:
        return directories[0]
    try:
        import os.path

        return Path(os.path.commonpath([str(d) for d in directories]))
    except ValueError:  # different drives on Windows
        return fallback


def render_up_config(
    target: Path | str | list,
    config_path: Path,
    *,
    name: str | None = None,
    port: int = 8765,
    profile: str = "quickstart",
) -> str:
    """Render the YAML for a laptop config over one or more targets.

    ``target`` accepts a plain path (the original single-source form, kept
    for callers and tests that predate multi-target ``up``) or a list of
    ``ResolvedTarget``s from :mod:`pheasant.targets`.

    Deterministic for a given (targets, config_path, options) tuple so a
    regenerated config is byte-identical — the idempotency bar every
    bootstrap command in this codebase has to clear.
    """
    from pheasant.targets import ResolvedTarget

    config_path = Path(config_path)
    local = state_root(config_path)

    if isinstance(target, (str, Path)):
        resolved = Path(target).resolve()
        source_type = detect_source_type(resolved)
        targets = [
            ResolvedTarget(
                name=name or slugify(resolved.name),
                type=source_type.value,
                path=str(resolved),
                description=f"Auto-detected {source_type.value}",
            )
        ]
    else:
        targets = list(target)
    if not targets:
        raise ValueError("at least one target is required")

    local_paths = [Path(t.path) for t in targets if t.local]
    workspace = _common_workspace(local_paths, config_path.resolve().parent)
    kb_name = name or (targets[0].name if len(targets) == 1 else slugify(workspace.name))

    data: dict[str, Any] = deep_merge(
        PheasantConfig().model_dump(mode="json"), profile_data(profile)
    )
    origin = targets[0].path if len(targets) == 1 else f"{len(targets)} sources"
    data["pheasant"].update(
        {
            "name": kb_name,
            "description": f"Personal knowledge base over {origin}",
            "state_path": str(local / "state"),
            "exports_path": str(local / "exports"),
            "workspace_root": str(workspace),
        }
    )
    data["server"]["port"] = port
    # A laptop install answers on loopback only. The API is unauthenticated
    # and can be pointed at any readable path, so the default 0.0.0.0 would
    # hand the whole index — and the filesystem behind it — to anyone on the
    # same network. Containers still bind 0.0.0.0 (see the Dockerfile:
    # binding loopback inside a container makes it unreachable from the
    # host); it is compose that publishes them to 127.0.0.1.
    data["server"]["host"] = "127.0.0.1"
    roots = [str(workspace)]
    for candidate in [*(str(p) for p in local_paths), str(local / "exports")]:
        if candidate not in roots:
            roots.append(candidate)
    data["security"]["allow_workspace_roots"] = roots
    data["sources"] = [target.to_source_dict() for target in targets]

    header_lines = [f"# Generated by `pheasant up` ({len(targets)} source(s))"]
    for entry in targets:
        header_lines.append(f"#   {entry.name}: {entry.type} <- {entry.path}")
    header_lines.append("# Edit freely — `pheasant up` never overwrites an existing config.")
    header = "\n".join(header_lines) + "\n"
    return header + dump_config_yaml(_scrub(data))


def ensure_up_config(
    target: Path | str | list,
    config_path: Path,
    *,
    name: str | None = None,
    port: int = 8765,
    profile: str = "quickstart",
) -> bool:
    """Write the generated config, or add missing targets to an existing one.

    Returns True when a new config was written, False otherwise. Any targets
    appended to an existing config are reported through :func:`added_sources`
    on the last call, so the CLI can say what it did.

    An existing config is never *rewritten* — settings the user edited are
    left exactly as they are, and re-running ``pheasant up`` with the same
    targets leaves the file byte-identical. But a target that is **not** in
    the config yet is appended, because "reusing the existing config" used to
    mean a flat `pheasant up ~/new-notes -c pheasant.yaml` silently indexed
    nothing and reported success — including straight after `pheasant setup`,
    whose own closing advice is to run exactly that.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            render_up_config(target, config_path, name=name, port=port, profile=profile),
            encoding="utf-8",
        )
        _LAST_ADDED.clear()
        return True
    _LAST_ADDED.clear()
    _LAST_ADDED.extend(_append_missing_sources(target, config_path))
    return False


#: Names appended by the most recent :func:`ensure_up_config` call. Module
#: state rather than a changed return type: three call sites and two tests
#: already treat the bool as "was it created", and that answer is still right.
_LAST_ADDED: list[str] = []


def added_sources() -> list[str]:
    """Source names the last ``ensure_up_config`` appended to an existing config."""
    return list(_LAST_ADDED)


def _append_missing_sources(target: Any, config_path: Path) -> list[str]:
    """Add any target not already configured. Returns the names added.

    Matching is by **resolved path**, not by name: the same directory reached
    through a symlink or a relative path is the same source, and adding it
    twice under two names would index it twice.
    """
    targets = target if isinstance(target, list) else [target]
    if not targets or not hasattr(targets[0], "to_source_dict"):
        # Called with a bare path (the pre-target-resolution shape used by
        # `render_up_config`); nothing to merge.
        return []
    try:
        existing = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    if not isinstance(existing, dict):
        return []

    sources = [s for s in (existing.get("sources") or []) if isinstance(s, dict)]
    known_paths = {_resolved(s.get("path")) for s in sources}
    known_names = {str(s.get("name")) for s in sources}

    added: list[str] = []
    for entry in targets:
        payload = entry.to_source_dict()
        if _resolved(payload.get("path")) in known_paths:
            continue
        name = payload["name"]
        if name in known_names:
            name = f"{name}-{len(sources) + 1}"
            payload["name"] = name
        sources.append(payload)
        known_paths.add(_resolved(payload.get("path")))
        known_names.add(name)
        added.append(name)
    if not added:
        # Byte-identical: re-running with the same targets must not rewrite.
        return []

    security = existing.setdefault("security", {})
    roots = list(security.get("allow_workspace_roots") or [])
    for entry in targets:
        if getattr(entry, "local", False) and str(entry.path) not in roots:
            roots.append(str(entry.path))
    if roots:
        security["allow_workspace_roots"] = roots

    existing["sources"] = sources
    config_path.write_text(dump_config_yaml(existing), encoding="utf-8")
    return added


def _resolved(path: Any) -> str:
    try:
        return str(Path(str(path)).expanduser().resolve())
    except (OSError, ValueError):
        return str(path)
