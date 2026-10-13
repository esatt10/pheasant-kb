"""Where a question or a search is allowed to look: a source, a folder or a
file inside it, minus documents named to be left out.

Three ways to say it, one resolution:

* **Parameters**, on every surface: ``source_name`` + ``path_prefix`` (a
  folder or a file), and ``exclude_documents`` — names, paths or artifact ids
  to leave out (``POST /search``, ``GET /documents``, ``POST /assistant/chat``
  and their MCP tools).
* **Keywords** in a question: ``@current`` (the open document), ``@dir`` (its
  folder, or the folder being browsed) and ``@exclude <names>``. A region
  keeps no UI state, so the first two are read against ``current_document``
  and ``current_directory``, which the caller sends — the web UI sends what
  its taxonomy view has open, and an agent sends whatever it is looking at.
* **The web UI's toggles**, which send the parameters.

An exclusion names *documents*, within the scope: ``deploy.md`` leaves out
every document in scope with that name or path tail, ``guide/deploy.md`` the
one at that path, and an artifact id exactly that one. A name that matches
only documents outside the scope needs nothing doing: they were never in it. Resolution runs under
the same ACL rule as every listing (``inventory.reader``), so whether a name
resolved never says anything about a document the caller may not read.

Over the API an unresolvable name is refused (``UNKNOWN_DOCUMENT``): a
program that asked to leave something out and was not told it failed would
measure a search it did not run. In a question it is said in a note instead,
the way ``@taxonomy`` reports a folder it could not find.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from typing import Any

from pheasant.ingestion.content_types import MEMORY_ARTIFACT_TYPE
from pheasant.search.criteria import Exclusion, path_scope, path_scope_sql
from pheasant.services import ServiceContext
from pheasant.services.errors import DocumentNotFound, InvalidRequest
from pheasant.services.inventory import ARTIFACT_SCOPE, reader

#: Names one request may exclude, and documents those names may expand to. A
#: longer list is a different question ("search only these"), and an
#: exclusion list this long turns every statement into a scan of it.
MAX_EXCLUDE_NAMES = 50
MAX_EXCLUDED_DOCUMENTS = 500


@dataclass(frozen=True)
class Scope:
    """A resolved scope: what every search and listing in one request may see."""

    source_name: str | None = None
    path_prefix: str | None = None
    #: ``file`` or ``folder`` when ``path_prefix`` is set.
    kind: str | None = None
    exclusion: Exclusion = field(default_factory=Exclusion)
    #: ``source/path`` of each document left out, for the answer to show.
    excluded: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    #: The keywords that set it (``@current``, ``@dir``, ``@exclude``).
    keywords: tuple[str, ...] = ()
    #: A scope keyword with nothing to read it against: the question must not
    #: be answered from a wider scope than the one it asked for.
    missing: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.path_prefix or self.exclusion or self.notes or self.keywords)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "path_prefix": self.path_prefix,
            "kind": self.kind,
            "excluded": list(self.excluded),
            "unresolved": list(self.unresolved),
            "notes": list(self.notes),
            "keywords": list(self.keywords),
            "missing": self.missing,
        }


def resolve(
    context: ServiceContext,
    *,
    source_name: str | None = None,
    path_prefix: str | None = None,
    exclude_documents: list[str] | None = None,
    principal: str | None = None,
    principal_groups: list[str] | None = None,
    strict: bool = True,
) -> Scope:
    """The scope a request's parameters name, exclusions resolved within it.

    ``strict`` refuses a name that resolves to nothing (the API); otherwise it
    is reported in ``unresolved`` and a note (a question).
    """

    prefix = path_scope(path_prefix) or None
    names = _names(exclude_documents)
    kind = _kind(context, source_name, prefix)
    scope = Scope(source_name=source_name, path_prefix=prefix, kind=kind)
    if not names:
        return scope
    if len(names) > MAX_EXCLUDE_NAMES:
        raise InvalidRequest(
            f"exclude_documents names {len(names)} documents; at most {MAX_EXCLUDE_NAMES}"
        )
    admit = reader(context, principal, principal_groups)
    rows: dict[str, Any] = {}
    unresolved: list[str] = []
    for name in names:
        found = _matches(context, name, source_name, prefix, admit)
        # A document outside the scope is already left out; only a name that
        # names nothing at all is a mistake worth saying.
        if not found and (
            not (source_name or prefix) or not _matches(context, name, None, None, admit)
        ):
            unresolved.append(name)
        for row in found:
            rows[str(row["id"])] = row
    if unresolved and strict:
        raise DocumentNotFound(f"{unresolved[0]} (no document by that name to exclude)")
    if len(rows) > MAX_EXCLUDED_DOCUMENTS:
        raise InvalidRequest(
            f"exclude_documents matches {len(rows)} documents; at most {MAX_EXCLUDED_DOCUMENTS}"
        )
    ordered = sorted(rows.values(), key=lambda r: (str(r["source_id"]), str(r["relative_path"])))
    exclusion = Exclusion(
        ids=frozenset(rows),
        pairs=frozenset((str(r["source_id"]), str(r["relative_path"])) for r in ordered),
    )
    notes = tuple(f"no document named “{name}” to leave out" for name in unresolved)
    return replace(
        scope,
        exclusion=exclusion,
        excluded=tuple(f"{r['source_id']}/{r['relative_path']}" for r in ordered),
        unresolved=tuple(unresolved),
        notes=notes,
    )


def resolve_keywords(
    context: ServiceContext,
    *,
    keyword: str | None,
    current_document: str | None,
    current_directory: str | None,
    source_name: str | None,
    path_prefix: str | None,
    exclude_documents: list[str] | None,
    used: tuple[str, ...] = (),
    principal: str | None = None,
    principal_groups: list[str] | None = None,
) -> Scope:
    """A question's scope: ``@current`` / ``@dir`` read against what the caller has open.

    ``keyword`` is ``document`` (``@current``), ``directory`` (``@dir``) or
    ``None``. A keyword with nothing to read it against is said in a note and
    the question keeps the scope its parameters gave it.
    """

    notes: list[str] = []
    missing: str | None = None
    if keyword == "document":
        located = locate(context, current_document, directory=False) if current_document else None
        if located:
            source_name, path_prefix = located
        else:
            held = f"; “{current_document}” is not one this region holds"
            missing = "@current"
            notes.append(
                "@current needs an open document (current_document)"
                + (held if current_document else "")
            )
    elif keyword == "directory":
        located = None
        if current_directory:
            located = locate(context, current_directory, directory=True)
        elif current_document:
            document = locate(context, current_document, directory=False)
            if document:
                located = (document[0], str(PurePosixPath(document[1]).parent).strip("."))
        if located:
            source_name, path_prefix = located[0], located[1] or None
        else:
            missing = "@dir"
            notes.append(
                "@dir needs a folder or an open document (current_directory or current_document)"
            )
    scope = resolve(
        context,
        source_name=source_name,
        path_prefix=path_prefix,
        exclude_documents=exclude_documents,
        principal=principal,
        principal_groups=principal_groups,
        strict=False,
    )
    return replace(scope, notes=(*notes, *scope.notes), keywords=used, missing=missing)


def locate(
    context: ServiceContext, locator: str | None, *, directory: bool
) -> tuple[str, str] | None:
    """``(source, path)`` for a document or folder locator, or ``None``.

    A locator is an artifact id (``file:{source}:{path}:branch={b}``),
    ``{source}/{path}``, or (for a folder) a bare source name for its root.
    A document must be one the region holds; a folder, one with a document
    under it.
    """

    text = str(locator or "").strip().strip("`'\"").strip()
    if not text:
        return None
    if text.startswith("file:") and ":branch=" in text:
        rows = context.state.rows(
            "SELECT source_id, relative_path FROM artifacts WHERE id=? LIMIT 1", (text,)
        )
        if not rows:
            return None
        source, path = str(rows[0]["source_id"]), str(rows[0]["relative_path"] or "")
        return (source, str(PurePosixPath(path).parent).strip(".")) if directory else (source, path)
    head, _, rest = text.lstrip("/").partition("/")
    if not context.state.get_source(head):
        return None
    path = path_scope(rest)
    if directory:
        if not path:
            return head, ""
        where, params = path_scope_sql(path, column="relative_path")
        rows = context.state.rows(
            f"SELECT 1 AS hit FROM artifacts WHERE source_id = ?{where} LIMIT 1", (head, *params)
        )
        return (head, path) if rows else None
    rows = context.state.rows(
        "SELECT 1 AS hit FROM artifacts WHERE source_id = ? AND relative_path = ? LIMIT 1",
        (head, path),
    )
    return (head, path) if rows else None


def _names(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.split(",")
    out: list[str] = []
    for value in raw:
        for part in str(value or "").split(","):
            name = part.strip().strip("`'\"“”").strip()
            if name and name not in out:
                out.append(name)
    return out


def _kind(context: ServiceContext, source_name: str | None, prefix: str | None) -> str | None:
    if not prefix:
        return None
    if source_name:
        rows = context.state.rows(
            "SELECT 1 AS hit FROM artifacts WHERE source_id = ? AND relative_path = ? LIMIT 1",
            (source_name, prefix),
        )
        return "file" if rows else "folder"
    return "folder"


def _matches(
    context: ServiceContext, name: str, source_name: str | None, prefix: str | None, admit: Any
) -> list[Any]:
    """Documents in scope that ``name`` names: an id, a path, a source/path or a tail."""

    acl = ", a.acl" if admit is not None else ""
    base = f"SELECT a.id, a.source_id, a.relative_path{acl} FROM artifacts a WHERE {ARTIFACT_SCOPE}"
    params: list[Any] = [MEMORY_ARTIFACT_TYPE]
    if source_name:
        base += " AND a.source_id = ?"
        params.append(source_name)
    where, scoped = path_scope_sql(prefix, column="a.relative_path")
    base += where
    params.extend(scoped)
    if name.startswith("file:") and ":branch=" in name:
        rows = context.state.rows(f"{base} AND a.id = ?", (*params, name))
    else:
        wanted = name.lower().strip("/")
        tail = wanted.rsplit("/", 1)[-1]
        rows = [
            row
            for row in context.state.rows(
                f"{base} AND LOWER(a.relative_path) LIKE ? ESCAPE '\\'",
                (*params, f"%{_escape_like(tail)}"),
            )
            if _named(row, wanted)
        ]
    return list(admit(rows) if admit is not None else rows)


def _named(row: Any, wanted: str) -> bool:
    relative = str(row["relative_path"] or "").lower()
    qualified = f"{str(row['source_id']).lower()}/{relative}"
    return wanted in {relative, qualified} or relative.endswith(f"/{wanted}")


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
