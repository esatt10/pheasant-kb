"""Content that may never enter the searchable corpus.

A sibling of `path_policy.py`, and the same kind of rule: that one says which
paths this process may *read*, this one says which paths may become
*retrievable*. They are separate questions — a benchmark answer key is a file
pheasant is perfectly entitled to read and must never be able to return.

The control exists for one job, and the job explains its shape. An experiment
scores a region against questions whose answers are held outside it; if an
answer key reaches the index, every number the experiment produces is
worthless and nothing about the run says so. Detection is therefore too late
by construction: by the time a check can observe a denylisted artifact in the
corpus, it has been retrievable. So the rule refuses entry.

**It lives here, at layer 1, because both entry points need it.** The first
version put it in `services/ingestion.py` and enforced it on submissions only
— which meant a region with a denylist configured still indexed a benchmark
file that arrived through a folder source, the UI drop zone, a git repository
or any connector, while the readiness gate reported the boundary intact. A
control enforced at one of several doors is not a control; it is a door with a
sign on it. `sync/` cannot import `services/` (that is an upward edge), so the
rule is here and both callers reach down to it.

**It returns the matching pattern rather than raising.** The two callers want
different things from a match: the submission path refuses the caller with
`ContaminationRefused`, and the sync path skips the item and counts it, because
a connector listing a thousand files is not making a request that can be
refused. A predicate that raised would force one of them to catch an exception
as flow control.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from fnmatch import fnmatch
from pathlib import PurePosixPath


def denied_by(relative: str, patterns: Sequence[str] | Iterable[str] | None) -> str | None:
    """The first denylist pattern this path matches, or ``None``.

    Matched against both the full relative path and the bare filename, because
    an operator writing ``*.answers.json`` means "anywhere" and one writing
    ``benchmark/*`` means "in that directory" — and requiring them to know
    which spelling this function prefers is how a denylist ends up not
    covering the thing it was written for.

    An empty or absent list costs one truth test, which is what keeps the
    no-configuration path unchanged (rule 7).
    """

    if not patterns:
        return None
    path = str(relative or "").replace("\\", "/").lstrip("/")
    if not path:
        return None
    name = PurePosixPath(path).name
    for pattern in patterns:
        if not pattern:
            continue
        if fnmatch(path, pattern) or fnmatch(name, pattern):
            return pattern
    return None


def denylist_of(config: object) -> tuple[str, ...]:
    """``readiness.corpus_denylist``, tolerating a config that predates it.

    Read through ``getattr`` rather than a typed access so a caller holding an
    older config object — a restored state directory, a worker built from a
    trimmed config — gets an empty list instead of an ``AttributeError`` on the
    indexing path.
    """

    settings = getattr(config, "readiness", None)
    return tuple(getattr(settings, "corpus_denylist", ()) or ())


#: Refusals listed individually in a sync report. A mis-written pattern can
#: match every file in a corpus and this rides in a `sync_events` row, so the
#: count is always exact and the list is a sample.
REFUSED_REPORT_LIMIT = 50


def refusal_details(refused: Sequence[tuple[str, str]]) -> dict[str, object]:
    """The denylist block of a sync report, present only when it says something.

    Absent on a region with no denylist, so that payload stays byte-identical
    to what it was before this control existed — the rule `heading_path` and
    `memory_policy` already follow.

    Here rather than in the engine because a policy owns how its refusals are
    reported: the shape and the rule change together, and splitting them is how
    a report ends up describing a rule nobody enforces any more.
    """

    if not refused:
        return {}
    return {
        "refused": [
            {"relative_path": path, "pattern": pattern}
            for path, pattern in refused[:REFUSED_REPORT_LIMIT]
        ],
        "refused_total": len(refused),
    }
