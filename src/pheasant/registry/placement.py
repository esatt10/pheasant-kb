"""Where a pasted target goes among the sources that already exist.

Quick add and ``pheasant up`` name a source after what was pasted: a folder
after its directory, a repository after its last path segment, a web page after
its host. Two different things can derive one name - ``/data/a/docs`` and
``/data/b/docs``, ``org1/tools`` and ``org2/tools``, two folders of one GitHub
repository - and registering under a name that was taken replaced the source
already there, dropping everything it had indexed. A web page derives the name
of every other page of its site, and there the shared name is right: the page
belongs in that collection.

So a target that meets an existing name does exactly one of three things:

* ``join``: a web or API page joins the collection named after its host;
* ``same``: it *is* that source (the same folder, file or checkout), which is
  re-added as it stands, keeping whatever was set on it since;
* ``new``: it is something else, and takes the first free ``name-2``,
  ``name-3`` …, with a checkout of its own if it is a repository.

``POST /sources`` and MCP ``register_source`` are not here: they name the
source themselves and set its whole configuration, so a name they give is a
replacement they asked for.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pheasant.targets import ResolvedTarget, free_name, joins_collection

Placement = Literal["join", "same", "new"]


def place(
    target: ResolvedTarget, existing: Any, taken: Callable[[str], bool]
) -> tuple[Placement, ResolvedTarget]:
    """What ``target`` does given the source ``existing`` under its name (or None)."""

    if existing is None:
        return "new", target
    if joins_collection(target.type, _get(existing, "type")):
        return "join", target
    if same_source(target, existing):
        return "same", target
    return "new", target.renamed(free_name(target.name, taken))


def place_all(
    targets: list[ResolvedTarget], live: Callable[[str], Any]
) -> list[tuple[Placement, ResolvedTarget, Any]]:
    """Place each target of one call; ``live(name)`` is the source under ``name``."""

    claimed = {target.name for target in targets}
    placed: list[tuple[Placement, ResolvedTarget, Any]] = []
    for target in targets:
        existing = live(target.name)
        kind, target = place(target, existing, lambda n: n in claimed or live(n) is not None)
        claimed.add(target.name)
        placed.append((kind, target, existing))
    return placed


def same_source(target: ResolvedTarget, existing: Any) -> bool:
    """Whether ``existing`` (a ``SourceConfig`` or its dict) is what ``target`` names."""

    kind = _get(existing, "type")
    if getattr(kind, "value", kind) != target.type:
        return False
    repo = _get(existing, "repo") or {}
    if target.clone_url or _get(repo, "clone_url"):
        if (_get(repo, "clone_url"), _get(repo, "clone_ref")) != (
            target.clone_url,
            target.clone_ref,
        ):
            return False
        # One checkout, two folders of it: the folder decides.
        return _relative(target.path, target.clone_path) == _relative(
            _get(existing, "path"), _get(repo, "clone_path")
        )
    return _resolved(_get(existing, "path")) == _resolved(target.path)


def _get(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def _relative(path: Any, root: Any) -> str:
    try:
        return Path(str(path)).relative_to(Path(str(root))).as_posix()
    except (TypeError, ValueError):
        return _resolved(path)


def _resolved(path: Any) -> str:
    try:
        return str(Path(str(path)).expanduser().resolve())
    except (OSError, ValueError):
        return str(path)
