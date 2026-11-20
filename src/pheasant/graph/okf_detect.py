"""Which directories of a source are Open Knowledge Format bundles.

Split from ``graph/okf.py`` (which plans a bundle's graph) because deciding
*whether* a directory is a bundle is its own question, answered over every
file of the source at once. Deterministic, OKF v0.2 §3, §8, §11, §12:

1. ``explicit``: a directory whose ``index.md`` declares ``okf_version``.
2. ``index``: a directory whose ``index.md`` is an OKF listing (no
   frontmatter, §8) and whose listed scope is conformant (below).
3. ``conformant_tree``: failing both, the shallowest directory whose subtree
   is conformant *and* corroborated by a key only an OKF producer writes
   (``sources``, ``generated``, ``verified``, ...) or by an OKF ``log.md``.

A scope is *conformant* when it holds at least ``MIN_CONCEPTS`` concepts and
at least ``MIN_CONCEPT_SHARE`` of its Markdown is concepts. For a directory
with an index, the scope is what the index lists (its entries' folders and
files, plus its own folder), so a repository that *is* a bundle is not sunk by
the developer docs beside it. Repository boilerplate (``README.md``,
``CHANGELOG.md`` ...) and hidden folders (``.github/`` ...) count either way.
The shallowest qualifying directory wins, and nothing nested in a bundle is a
second bundle.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Any

from pheasant.ingestion.okf import INDEX_FILENAME, LOG_FILENAME

MIN_CONCEPTS = 2
MIN_CONCEPT_SHARE = 0.8

#: Files a repository carries whatever it holds. Neither concepts nor
#: evidence against a bundle.
BOILERPLATE_FILENAMES = frozenset(
    {
        "readme.md",
        "changelog.md",
        "contributing.md",
        "license.md",
        "code_of_conduct.md",
        "security.md",
    }
)


@dataclass(frozen=True)
class SourceArtifact:
    """One artifact of the source, as the planner needs it."""

    node_id: str
    relative_path: str
    okf: dict[str, Any] | None


@dataclass(frozen=True)
class Bundle:
    root: str
    detection: str
    okf_version: str | None


@dataclass
class _DirStats:
    concepts: int = 0
    others: int = 0
    signals: int = 0
    has_log: bool = False


def detect_bundles(artifacts: list[SourceArtifact]) -> list[Bundle]:
    """The bundle roots in one source, shallowest first. Pure and deterministic."""

    # Hidden directories (`.github/`, `.changeset/`) are a repository's
    # tooling: neither concepts nor evidence against a bundle.
    markdown = [
        a
        for a in artifacts
        if a.relative_path.lower().endswith(".md")
        and not any(part.startswith(".") for part in _norm(a.relative_path).split("/"))
    ]
    stats: dict[str, _DirStats] = {}
    listings: dict[str, dict[str, Any]] = {}
    #: Every counted Markdown file as (path, is concept), for a listing's scope.
    counted: list[tuple[str, bool]] = []
    for artifact in markdown:
        path = _norm(artifact.relative_path)
        name = posixpath.basename(path).lower()
        role = (artifact.okf or {}).get("role")
        directory = posixpath.dirname(path)
        if name == INDEX_FILENAME:
            if role == "index":
                listings[directory] = artifact.okf or {}
            continue
        if name == LOG_FILENAME:
            if role == "log":
                for ancestor in _ancestors(directory):
                    stats.setdefault(ancestor, _DirStats()).has_log = True
            continue
        if name in BOILERPLATE_FILENAMES:
            continue
        is_concept = role == "concept"
        counted.append((path, is_concept))
        signalled = bool((artifact.okf or {}).get("signals")) if is_concept else False
        for ancestor in _ancestors(directory):
            entry = stats.setdefault(ancestor, _DirStats())
            if is_concept:
                entry.concepts += 1
                entry.signals += int(signalled)
            else:
                entry.others += 1

    def conformant(directory: str, minimum: int = MIN_CONCEPTS) -> bool:
        entry = stats.get(directory)
        if entry is None or entry.concepts < minimum:
            return False
        return entry.concepts / (entry.concepts + entry.others) >= MIN_CONCEPT_SHARE

    def listed_conformant(directory: str, minimum: int = MIN_CONCEPTS) -> bool:
        """Conformance over what a directory's index lists, when it lists anything.

        A repository that *is* a bundle carries developer docs beside its
        concepts, and measured over everything beneath the root they sink it
        below the share. The index is the bundle's own statement of what it
        holds (OKF §8), so the share is taken over the folders and files its
        entries name, plus the index's own folder. An index that lists nothing
        inside the tree falls back to the whole subtree.
        """

        folders, files = _listed_scope(directory, listings.get(directory) or {})
        if not folders and not files:
            return conformant(directory, minimum)
        concepts = others = 0
        for path, is_concept in counted:
            if not _within(posixpath.dirname(path), directory):
                continue
            inside = (
                posixpath.dirname(path) == directory
                or path in files
                or any(_within(posixpath.dirname(path), folder) for folder in folders)
            )
            if not inside:
                continue
            concepts += int(is_concept)
            others += int(not is_concept)
        if concepts < minimum:
            return False
        return concepts / (concepts + others) >= MIN_CONCEPT_SHARE

    roots: list[Bundle] = []

    def claimed(directory: str) -> bool:
        return any(_within(directory, bundle.root) for bundle in roots)

    explicit = sorted(
        (d for d, listing in listings.items() if listing.get("okf_version")), key=_depth_key
    )
    for directory in explicit:
        if not claimed(directory) and listed_conformant(directory, minimum=1):
            roots.append(Bundle(directory, "explicit", str(listings[directory]["okf_version"])))
    for directory in sorted(listings, key=_depth_key):
        if not claimed(directory) and listed_conformant(directory):
            roots.append(Bundle(directory, "index", None))
    for directory in sorted(stats, key=_depth_key):
        if claimed(directory) or any(_within(bundle.root, directory) for bundle in roots):
            continue
        entry = stats[directory]
        if conformant(directory) and (entry.signals or entry.has_log):
            roots.append(Bundle(directory, "conformant_tree", None))
    return sorted(roots, key=lambda bundle: _depth_key(bundle.root))


def _is_external(target: str) -> bool:
    return "://" in target or target.lower().startswith(("mailto:", "data:"))


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _listed_scope(directory: str, listing: dict[str, Any]) -> tuple[set[str], set[str]]:
    """The folders and files an index's entries name inside its own tree."""

    folders: set[str] = set()
    files: set[str] = set()
    for entry in listing.get("entries") or []:
        target = str(entry.get("target") or "").split("#", 1)[0].strip()
        if not target or _is_external(target):
            continue
        # A leading `/` is bundle-relative (§6.1), and this index's folder is
        # the candidate bundle root.
        joined = posixpath.join(directory, target.lstrip("/"))
        resolved = _norm(posixpath.normpath(joined)) if joined else ""
        resolved = "" if resolved == "." else resolved
        if not _within(
            posixpath.dirname(resolved) if resolved.endswith(".md") else resolved, directory
        ):
            continue
        if resolved.lower().endswith(".md"):
            if posixpath.basename(resolved).lower() == INDEX_FILENAME:
                folders.add(posixpath.dirname(resolved))
            else:
                files.add(resolved)
        elif resolved and resolved != directory:
            folders.add(resolved)
    return folders, files


def _ancestors(directory: str) -> list[str]:
    """``a/b`` → ``["", "a", "a/b"]``: every directory a file is under."""

    parts = [part for part in directory.split("/") if part]
    return [""] + ["/".join(parts[: index + 1]) for index in range(len(parts))]


def _within(directory: str, root: str) -> bool:
    return not root or directory == root or directory.startswith(root + "/")


def _depth_key(directory: str) -> tuple[int, str]:
    return (0 if not directory else directory.count("/") + 1, directory)
