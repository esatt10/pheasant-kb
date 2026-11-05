"""Keep every artifact's source date true of the source, without re-indexing.

``artifacts.source_modified_at`` is written with the content when a file is
indexed, and that alone would leave it wrong in three ordinary cases:

* **An upgrade.** Rows indexed before the column existed hold NULL, and an
  incremental sync skips them because their bytes are unchanged -- so they
  would stay undated for as long as nobody edited them.
* **A date that moves without the bytes moving.** A file touched, a Drive
  document renamed, an API record re-stamped. The source's answer to "when did
  this last change" moved; the sha256 did not.
* **A repository.** The file's mtime in a checkout is when it was checked
  out, so a fresh clone dates every file to the clone. The repository's own
  answer is the last commit that touched the path.

:func:`reconcile` runs once per sync, after the pass, and writes only the rows
whose stored date differs from what the source says now. An unchanged corpus
reads one column and writes nothing. It never writes ``NULL`` over a known
date: a connector that does not report one at listing time (the web
connector learns ``Last-Modified`` only by fetching) leaves the value the
fetch recorded alone.

Deterministic, like the rest of the sync path: git history and the listing
are the only inputs, and no clock reaches the column.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pheasant.ingestion.timestamps import normalize_timestamp
from pheasant.persistence import artifact_dates

logger = logging.getLogger(__name__)

#: How long one history walk may take before the paths it has not reached are
#: left undated for this sync. Bounded because the walk can be the whole
#: history on a first index; fail-soft because a date is metadata, and a sync
#: must never fail over one.
GIT_LOG_TIMEOUT_SECONDS = 120.0

#: `sync_fingerprints` scope recording the HEAD whose history walk finished.
#: A path that walk did not find has no date in this history (a shallow clone's
#: boundary), and asking again at the same HEAD would repeat the whole walk on
#: every sync for an answer that cannot change.
GIT_WALK_SCOPE = "source:{name}:git_dates"


def _git(root: Path, *args: str, timeout: float = 30.0) -> str | None:
    try:
        return subprocess.run(
            ["git", "-c", "core.quotePath=false", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        ).stdout
    except Exception:  # noqa: BLE001 - no git, not a repository, a timeout
        return None


def _shallow_boundaries(root: Path) -> set[str]:
    """The commits a shallow clone was cut at.

    ``git log --name-only`` reports every file present at a boundary commit as
    *added* there, because the commit has no parent in the clone. Attributing
    those files to it would date most of a shallow repository to whatever
    commit the clone happened to stop at, which is a confident wrong answer.
    """

    path = _git(root, "rev-parse", "--git-path", "shallow")
    if not path:
        return set()
    shallow = Path(path.strip())
    if not shallow.is_absolute():
        shallow = root / shallow
    try:
        return {line.strip() for line in shallow.read_text().splitlines() if line.strip()}
    except OSError:
        return set()


def git_commit_dates(
    root: Path,
    wanted: Iterable[str],
    *,
    timeout: float = GIT_LOG_TIMEOUT_SECONDS,
) -> tuple[dict[str, str], bool]:
    """``({relative path: last commit date}, walk_complete)`` for ``wanted``.

    Paths are relative to ``root`` (``--relative``), which is the source's own
    path and may be a subdirectory of the repository. One ``git log`` streams
    newest first and stops as soon as every wanted path is dated, so an
    incremental sync -- whose changed files were changed recently -- reads a
    few commits. The committer date is used: it is when the change landed on
    the branch being indexed, which is what "last updated" means to a reader of
    that branch. Merge commits list no files without ``-m``, so a change that
    arrived by merge is dated by the commit that made it.

    ``walk_complete`` is ``False`` when the walk was cut short (timeout, git
    missing), so the caller can tell "not in this history" from "not reached".
    """

    remaining = {str(path) for path in wanted if path}
    if not remaining:
        return {}, True
    boundaries = _shallow_boundaries(root)
    dates: dict[str, str] = {}
    try:
        process = subprocess.Popen(
            [
                "git",
                "-c",
                "core.quotePath=false",
                "-C",
                str(root),
                "log",
                "--no-renames",
                "--relative",
                "--name-only",
                "--format=%x1e%H %ct",
                "HEAD",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:  # noqa: BLE001 - no git binary
        return {}, False
    deadline = time.monotonic() + timeout
    complete = True
    current: str | None = None
    skip = False
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if time.monotonic() > deadline:
                complete = False
                break
            line = line.rstrip("\n")
            if line.startswith("\x1e"):
                commit, _, epoch = line[1:].partition(" ")
                skip = commit in boundaries
                current = normalize_timestamp(epoch) if not skip else None
                continue
            if not line or skip or current is None or line not in remaining:
                continue
            dates[line] = current
            remaining.discard(line)
            if not remaining:
                break
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
    if remaining and complete and process.returncode != 0:
        # The stream ended because git failed (an unborn HEAD, a broken
        # repository), not because history did: no claim the walk finished.
        complete = False
    return dates, complete


def git_worktree_changes(root: Path) -> set[str] | None:
    """Paths under ``root`` whose working copy differs from HEAD, or are untracked.

    Their last commit says nothing about what is on disk -- which is what was
    indexed -- so they are dated by their file's mtime instead. ``None`` when
    git cannot say, which the caller treats as "trust the commit dates".
    """

    prefix = _git(root, "rev-parse", "--show-prefix")
    status = _git(root, "status", "--porcelain", "-z", "--untracked-files=all", "--", ".")
    if prefix is None or status is None:
        return None
    prefix = prefix.strip()
    changed: set[str] = set()
    entries = status.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in {"R", "C"}:
            index += 1  # the rename's original path follows; it no longer exists
        if prefix and path.startswith(prefix):
            path = path[len(prefix) :]
        changed.add(path)
    return changed


def listing_dates(source_type: str, items: Iterable[Any]) -> dict[str, str]:
    """``{relative path: canonical date}`` the listing itself reports.

    A repository's listing mtimes are checkout times, so they are not source
    dates there and :func:`reconcile` asks git instead.
    """

    if source_type == "repository":
        return {}
    dates: dict[str, str] = {}
    for item in items:
        normalized = normalize_timestamp(getattr(item, "mtime", None))
        if normalized:
            dates[str(item.relative_path)] = normalized
    return dates


def reconcile(
    engine: Any,
    source: Any,
    items: list[Any],
    *,
    head_commit: str | None = None,
) -> int:
    """Write each artifact's current source date where it differs. Returns rows updated.

    Fail-soft: a failure here is logged and costs the dates, never the sync.
    """

    try:
        return _reconcile(engine, source, items, head_commit=head_commit)
    except Exception:  # noqa: BLE001 - metadata must never fail a sync
        logger.warning("%s: could not reconcile source dates", source.name, exc_info=True)
        return 0


def _reconcile(engine: Any, source: Any, items: list[Any], *, head_commit: str | None) -> int:
    state = engine.state
    rows = state.rows(
        "SELECT id, relative_path, source_modified_at FROM artifacts WHERE source_id=?",
        (source.name,),
    )
    if not rows:
        return 0
    stored = {
        str(row["relative_path"]): (str(row["id"]), row["source_modified_at"]) for row in rows
    }
    listed = {str(item.relative_path): item for item in items}
    source_type = str(getattr(source.type, "value", source.type))
    dates = listing_dates(source_type, items)
    if source_type == "repository" and source.path:
        dates.update(_repository_dates(engine, source, listed, stored, head_commit))
    updates = [
        (date, stored[path][0])
        for path, date in sorted(dates.items())
        if path in stored and stored[path][1] != date
    ]
    if not updates:
        return 0
    with engine._sync_mutex:
        artifact_dates.set_source_dates(state, updates)
    return len(updates)


def git_tracked(root: Path) -> set[str] | None:
    """Every path under ``root`` git tracks at HEAD, relative to ``root``."""

    out = _git(root, "ls-files", "-z", "--", ".")
    if out is None:
        return None
    return {path for path in out.split("\0") if path}


def git_changed_between(root: Path, old: str, new: str) -> set[str] | None:
    """Paths under ``root`` that differ between two commits; ``None`` if git cannot say."""

    out = _git(root, "diff", "--no-renames", "--relative", "--name-only", "-z", old, new)
    if out is None:
        return None
    return {path for path in out.split("\0") if path}


def _tracked_key(path: str, tracked: set[str]) -> str | None:
    """The path git dates for a listed item: itself, or the archive it came out of.

    An archive member (``bundle.zip/a.txt``) is no path git knows; it changed
    when its archive did, so it takes the archive's date.
    """

    if path in tracked:
        return path
    parts = path.split("/")
    for cut in range(len(parts) - 1, 0, -1):
        candidate = "/".join(parts[:cut])
        if candidate in tracked:
            return candidate
    return None


def _repository_dates(
    engine: Any,
    source: Any,
    listed: dict[str, Any],
    stored: dict[str, tuple[str, Any]],
    head_commit: str | None,
) -> dict[str, str]:
    """Commit dates for a repository's paths, asking git only what could have moved.

    A committed path's last-commit date changes only when a commit touches it,
    so after the first walk the question is ``git diff <walked HEAD> <HEAD>``
    plus whatever is still undated -- an incremental sync reads a handful of
    commits. Untracked and locally modified files are dated by their mtime,
    because their last commit says nothing about the bytes that were indexed.
    """

    root = Path(source.path)
    local = git_worktree_changes(root) or set()
    dates: dict[str, str] = {}
    for path in local:
        item = listed.get(path)
        normalized = normalize_timestamp(getattr(item, "mtime", None)) if item else None
        if normalized:
            dates[path] = normalized
    tracked = git_tracked(root)
    if not tracked:
        return dates
    scope = GIT_WALK_SCOPE.format(name=source.name)
    walked = engine.state.get_fingerprint(scope)
    moved: set[str] | None = None
    if walked and head_commit and walked != head_commit:
        moved = git_changed_between(root, walked, head_commit)
    elif walked and walked == head_commit:
        moved = set()
    # {git path: [listed paths it dates]}; several when members share an archive.
    wanted: dict[str, list[str]] = {}
    for path in listed:
        if path not in stored or path in local:
            continue
        key = _tracked_key(path, tracked)
        if key is None:
            continue
        if moved is None or key in moved or not stored[path][1]:
            wanted.setdefault(key, []).append(path)
    if not wanted:
        return dates
    found, complete = git_commit_dates(root, wanted)
    for key, date in found.items():
        for path in wanted[key]:
            dates[path] = date
    if complete and head_commit:
        from pheasant.ingestion.pipeline import utc_now

        engine.state.set_fingerprint(scope, head_commit, utc_now())
    elif not complete:
        logger.info(
            "%s: git history walk stopped before dating %d path(s); they stay undated "
            "until the next sync",
            source.name,
            len(wanted) - len(found),
        )
    return dates
