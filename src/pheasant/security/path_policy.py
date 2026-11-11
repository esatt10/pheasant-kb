from __future__ import annotations

from pathlib import Path
from typing import Any


class PathPolicyError(ValueError):
    pass


def resolve_under(path: str | Path, allowed_roots: list[str | Path]) -> Path:
    candidate = Path(path).expanduser().resolve()
    roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    if not roots:
        return candidate
    for root in roots:
        if candidate == root or root in candidate.parents:
            return candidate
    raise PathPolicyError(
        f"Path {candidate} is outside allowed roots: {', '.join(map(str, roots))}. "
        f"Mount it under one of those roots, or add its container path to "
        f"security.allow_workspace_roots (or set "
        f"security.allow_user_selected_source_paths: true)."
    )


def resolve_config_write_target(
    requested: str | Path | None,
    *,
    server_config_path: str | Path,
    allowed_roots: list[str | Path] | None = None,
) -> Path:
    """Where a "promote this source into YAML" write is allowed to land.

    ``config_path`` on the promote surfaces (HTTP ``POST
    /sources/{id}/promote``, MCP ``promote_runtime_source_to_config``) used
    to be written verbatim, which turned a source-management call into an
    arbitrary file write anywhere the process could reach. Promotion only
    ever *means* "write my config", so the target is constrained to the
    config file this server was started with, or — for the multi-config
    workflows the CLI supports — a path under an allowed root.

    ``allowed_roots`` is opt-in; with none supplied the server's own config
    path is the single permitted target.
    """
    server_path = Path(server_config_path).expanduser().resolve()
    if requested is None or str(requested).strip() == "":
        return server_path
    candidate = Path(requested).expanduser().resolve()
    if candidate == server_path:
        return candidate
    if allowed_roots:
        try:
            return resolve_under(candidate, list(allowed_roots))
        except PathPolicyError:
            pass
    raise PathPolicyError(
        f"Refusing to write config to {candidate}: promotion may only write this "
        f"server's config file ({server_path})"
        + (
            f" or a path under {', '.join(str(Path(r)) for r in allowed_roots)}."
            if allowed_roots
            else "."
        )
    )


def allowed_roots(config: Any) -> list[Path]:
    """Roots a UI or agent may browse and register sources under.

    The workspace, the exports path and ``security.allow_workspace_roots``,
    plus ``/`` where the region lets a person choose any path. Roots that do
    not exist are dropped: they can never contain a selectable path.
    """

    roots = [
        config.pheasant.workspace_root,
        config.pheasant.exports_path,
        *config.security.allow_workspace_roots,
    ]
    if config.security.allow_user_selected_source_paths:
        roots.append(Path("/"))
    seen: list[Path] = []
    for root in roots:
        resolved = Path(root).expanduser().resolve()
        if resolved not in seen and resolved.exists():
            seen.append(resolved)
    return seen


def resolve_source_path(path: str, config: Any) -> Path:
    """A source path a caller supplied, held to the region's path policy."""

    if config.security.allow_user_selected_source_paths:
        resolved = Path(path).expanduser().resolve()
        if not resolved.exists():
            raise PathPolicyError(f"Path does not exist: {resolved}")
        return resolved
    return resolve_under(path, allowed_roots(config))
