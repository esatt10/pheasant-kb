"""Where a search, a listing or an answer may look (`services.scope`).

Four things are held here:

1. **Exclusion by name.** ``exclude_documents`` leaves documents out of every
   arm by a file name, a path, a ``source/path`` or an artifact id; a name
   that matches nothing is refused over the API (one text on both surfaces)
   and said in a note in a question, and one that matches only documents
   outside the scope needs nothing doing.
2. **The keywords.** ``@current`` / ``@dir`` / ``@exclude`` count anywhere in
   a message and are read against what the caller has open
   (``current_document`` / ``current_directory``). With nothing open the
   answer says so instead of searching everything.
3. **They narrow the index lookups too** — ``@current @doc``, ``@dir @docs``,
   ``@pheasant how many documents @dir`` — which are answered with no search.
4. **Both backends** resolve and apply an exclusion the same way.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from pheasant.assistant import keywords
from pheasant.search.criteria import Exclusion, path_of, path_within
from pheasant.services import ServiceContext, scope
from pheasant.services.errors import DocumentNotFound, InvalidRequest, ServiceError
from tests.test_source_browser import _region

OPEN = {"current_document": "docs/guide/rotation.txt", "current_directory": "docs/guide"}
QUERY = "vault rotation installer runbook gateway port guidebook"


@pytest.fixture(scope="module")
def region(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _region(tmp_path_factory.mktemp("scope"))


def _context(region: dict[str, Any]) -> ServiceContext:
    return region["tools"].services


def _paths(payload: dict[str, Any]) -> list[str]:
    return [path_of(item) for item in payload["results"]]


def _ask(region: dict[str, Any], question: str, **kwargs: Any) -> dict[str, Any]:
    return region["tools"].ask_knowledge_base(region["kb"], question, **kwargs)


# ---------------------------------------------------------------------------
# 1. Reading the keywords
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "text", "where", "exclude"),
    [
        ("@dir how do I restart", "how do I restart", "directory", ()),
        ("how do I restart @current", "how do I restart", "document", ()),
        ("@this, what is it", "what is it", "document", ()),
        # The narrower wins.
        ("@dir @current hi", "hi", "document", ()),
        ("@exclude deploy.md restart", "restart", None, ("deploy.md",)),
        ("@exclude a.md,b.md hi", "hi", None, ("a.md", "b.md")),
        ("@omit a.md, guide/b.md hi", "hi", None, ("a.md", "guide/b.md")),
        # After `, ` a further name must look like a file.
        ("@exclude a.md, what changed", "what changed", None, ("a.md",)),
        ('what @without "my notes.md" says', "what says", None, ("my notes.md",)),
        # The rest is the ordinary leading-keyword reader's: `@docs` is read.
        ("@dir @exclude deploy.md @docs", "", "directory", ("deploy.md",)),
        # Email addresses and words are text.
        ("mail ops@dir.example now", "mail ops@dir.example now", None, ()),
        ("what does exclude mean", "what does exclude mean", None, ()),
    ],
)
def test_scope_keywords_count_anywhere(
    message: str, text: str, where: str | None, exclude: tuple[str, ...]
) -> None:
    read = keywords.read(message)

    assert read.scope == where
    assert read.exclude == exclude
    assert read.text == text


def test_the_catalog_lists_the_scope_keywords_as_anywhere() -> None:
    rows = {row["keyword"]: row for row in keywords.catalog()}

    for name in ("@current", "@dir", "@exclude"):
        assert rows[name]["kind"] == "scope" and rows[name]["anywhere"] is True, name
    assert "@this" in rows["@current"]["aliases"]


# ---------------------------------------------------------------------------
# 2. Resolving a scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("names", "within", "excluded"),
    [
        (["deploy.md"], None, ["docs/guide/deploy.md"]),
        (["DEPLOY.md"], None, ["docs/guide/deploy.md"]),
        (["guide/deploy.md"], None, ["docs/guide/deploy.md"]),
        (["docs/guide/deploy.md"], None, ["docs/guide/deploy.md"]),
        (["file:docs:guide/deploy.md:branch=main"], None, None),
        (["deploy.md, table.csv"], None, ["docs/data/table.csv", "docs/guide/deploy.md"]),
        # A name that is a folder's tail but no file's is not a document.
        (["guide"], None, []),
        # Outside a folder scope: nothing to do, and nothing refused.
        (["table.csv"], "guide", []),
    ],
)
def test_a_name_resolves_to_the_documents_it_names(
    region: dict[str, Any], names: list[str], within: str | None, excluded: list[str] | None
) -> None:
    context = _context(region)
    if excluded is None:
        # An artifact id: whatever branch label the folder source used.
        ids = context.state.rows(
            "SELECT id FROM artifacts WHERE relative_path = ?", ("guide/deploy.md",)
        )
        names, excluded = [str(ids[0]["id"])], ["docs/guide/deploy.md"]
    if excluded == [] and within is None:
        with pytest.raises(DocumentNotFound, match="no document by that name to exclude"):
            scope.resolve(context, source_name="docs", exclude_documents=names)
        return
    found = scope.resolve(context, source_name="docs", path_prefix=within, exclude_documents=names)

    assert list(found.excluded) == excluded
    assert len(found.exclusion.ids) == len(excluded)
    assert found.unresolved == ()


def test_a_name_that_matches_nothing_is_a_note_in_a_question(region: dict[str, Any]) -> None:
    found = scope.resolve(
        _context(region), source_name="docs", exclude_documents=["nothere.md"], strict=False
    )

    assert found.unresolved == ("nothere.md",)
    assert found.notes == ("no document named “nothere.md” to leave out",)
    assert not found.exclusion


def test_an_exclusion_list_is_bounded(region: dict[str, Any]) -> None:
    names = [f"n{n}.md" for n in range(scope.MAX_EXCLUDE_NAMES + 1)]

    with pytest.raises(InvalidRequest, match="at most 50"):
        scope.resolve(_context(region), exclude_documents=names)


@pytest.mark.parametrize(
    ("locator", "directory", "expected"),
    [
        ("docs/guide/rotation.txt", False, ("docs", "guide/rotation.txt")),
        ("docs/guide", True, ("docs", "guide")),
        ("docs", True, ("docs", "")),
        ("docs/guide/rotation.txt", True, ("docs", "guide/rotation.txt")),
        ("docs/guide", False, None),
        ("docs/nowhere", True, None),
        ("elsewhere/guide", True, None),
        ("", True, None),
    ],
)
def test_what_current_and_dir_are_read_against(
    region: dict[str, Any], locator: str, directory: bool, expected: tuple[str, str] | None
) -> None:
    assert scope.locate(_context(region), locator, directory=directory) == expected


def test_an_exclusion_admits_by_id_or_by_source_and_path() -> None:
    rule = Exclusion(ids=frozenset({"n1"}), pairs=frozenset({("docs", "a.md")}))

    assert not rule.admits({"node_id": "n1"})
    assert not rule.admits({"provenance": {"source_id": "docs", "relative_path": "a.md"}})
    assert not rule.admits({"source_id": "docs", "relative_path": "a.md"})
    assert rule.admits({"source_id": "docs", "relative_path": "b.md"})
    assert rule.admits({"source_id": "other", "relative_path": "a.md"})
    assert not Exclusion()


# ---------------------------------------------------------------------------
# 3. Search and listings over both surfaces
# ---------------------------------------------------------------------------


def test_an_excluded_document_leaves_every_arm(region: dict[str, Any]) -> None:
    tools, kb = region["tools"], region["kb"]
    plain = tools.search_context(kb, QUERY, source_name="docs")
    left = tools.search_context(
        kb, QUERY, source_name="docs", exclude_documents=["rotation.txt", "guidebook.txt"]
    )
    over_http = region["client"].post(
        "/search",
        json={
            "query": QUERY,
            "source_name": "docs",
            "exclude_documents": ["rotation.txt", "guidebook.txt"],
        },
    )

    assert {"guide/rotation.txt", "guidebook.txt"} <= set(_paths(plain))
    assert not {"guide/rotation.txt", "guidebook.txt"} & set(_paths(left)), _paths(left)
    assert left["results"], "the rest of the source must still answer"
    assert left["scope"]["excluded"] == ["docs/guide/rotation.txt", "docs/guidebook.txt"]
    assert over_http.status_code == 200
    assert _paths(over_http.json()) == _paths(left)
    assert over_http.json()["scope"] == left["scope"]


def test_an_exclusion_inside_a_folder_scope(region: dict[str, Any]) -> None:
    left = region["tools"].search_context(
        region["kb"],
        QUERY,
        source_name="docs",
        path_prefix="guide",
        exclude_documents=["deploy.md"],
    )

    assert left["results"] and all(path_within(p, "guide") for p in _paths(left))
    assert "guide/deploy.md" not in _paths(left)


def test_no_exclusion_changes_nothing(region: dict[str, Any]) -> None:
    tools, kb = region["tools"], region["kb"]
    plain = tools.search_context(kb, QUERY, source_name="docs")
    empty = tools.search_context(kb, QUERY, source_name="docs", exclude_documents=[])

    assert _paths(plain) == _paths(empty)
    assert "scope" not in plain and "scope" not in empty


def test_both_surfaces_refuse_an_unknown_name_with_one_text(region: dict[str, Any]) -> None:
    response = region["client"].post(
        "/search", json={"query": "vault", "exclude_documents": ["nothere.md"]}
    )
    listed = region["client"].get("/documents", params={"exclude_document": "nothere.md"})
    with pytest.raises(ServiceError) as refused:
        region["tools"].search_context(region["kb"], "vault", exclude_documents=["nothere.md"])

    assert response.status_code == listed.status_code == 404
    assert response.json()["detail"] == listed.json()["detail"] == str(refused.value)
    assert "nothere.md" in str(refused.value)


def test_a_listing_narrows_to_a_folder_less_its_exclusions(region: dict[str, Any]) -> None:
    tools, kb = region["tools"], region["kb"]
    listed = tools.list_documents(
        kb, source_name="docs", path_prefix="guide", exclude_documents=["deploy.md"]
    )
    over_http = region["client"].get(
        "/documents",
        params={"source_name": "docs", "path_prefix": "guide", "exclude_document": "deploy.md"},
    )

    paths = [row["path"] for row in listed["documents"]]
    assert paths == ["guide/Setup/install.md", "guide/rotation.txt"]
    assert listed["scope"]["excluded"] == ["docs/guide/deploy.md"]
    assert over_http.status_code == 200
    assert [row["path"] for row in over_http.json()["documents"]] == paths


# ---------------------------------------------------------------------------
# 4. Questions
# ---------------------------------------------------------------------------


def test_dir_answers_from_the_open_folder(region: dict[str, Any]) -> None:
    answer = _ask(region, "@dir how do I restart the vault after rotation", **OPEN)
    over_http = region["client"].post(
        "/assistant/chat",
        json={"question": "@dir how do I restart the vault after rotation", **OPEN},
    )

    cited = [c.get("relative_path") for c in answer["citations"]]
    assert cited and all(path_within(p, "guide") for p in cited), cited
    assert answer["scope"]["path_prefix"] == "guide" and answer["scope"]["keywords"] == ["@dir"]
    assert over_http.status_code == 200
    assert [c.get("relative_path") for c in over_http.json()["citations"]] == cited


def test_dir_without_a_folder_uses_the_open_documents(region: dict[str, Any]) -> None:
    answer = _ask(region, "@dir restart the vault", current_document="docs/guide/Setup/install.md")

    assert answer["scope"]["path_prefix"] == "guide/Setup"
    cited = [c.get("relative_path") for c in answer["citations"]]
    assert all(path_within(p, "guide/Setup") for p in cited), cited


def test_exclude_in_a_question_leaves_the_document_out(region: dict[str, Any]) -> None:
    question = "restart the vault after rotation"
    plain = _ask(region, question, source_name="docs")
    left = _ask(region, f"@exclude deploy.md {question}", source_name="docs")
    by_parameter = _ask(region, question, source_name="docs", exclude_documents=["deploy.md"])

    def cited(answer: dict[str, Any]) -> list[Any]:
        return [c.get("relative_path") for c in answer["citations"]]

    assert "guide/deploy.md" in cited(plain)
    assert "guide/deploy.md" not in cited(left)
    assert cited(left) == cited(by_parameter)
    assert left["scope"]["excluded"] == ["docs/guide/deploy.md"]


def test_a_name_that_matches_nothing_is_said_and_the_question_answered(
    region: dict[str, Any],
) -> None:
    answer = _ask(region, "@exclude nothere.md restart the vault", source_name="docs")

    assert answer["citations"]
    assert answer["scope"]["unresolved"] == ["nothere.md"]
    assert "no document named “nothere.md” to leave out" in answer["answer"]


@pytest.mark.parametrize("message", ["@current what is this", "@dir what is here"])
def test_a_scope_keyword_with_nothing_open_is_said_not_searched(
    region: dict[str, Any], message: str
) -> None:
    answer = _ask(region, message)

    assert answer["route"]["intent"] == "inventory"
    assert answer["citations"] == []
    assert answer["scope"]["missing"] == message.split()[0]
    assert (
        answer["answer"].count("needs an open document") + answer["answer"].count("needs a folder")
        == 1
    )


@pytest.mark.parametrize(
    ("message", "action", "check"),
    [
        ("@current @doc", "document", "docs/guide/rotation.txt"),
        ("@current @links", "links", "docs/guide/rotation.txt"),
        ("@dir @docs", "documents", "**3 documents** in `docs/guide`"),
        ("@dir @exclude deploy.md @docs", "documents", "leaving out 1 document"),
        ("@pheasant how many documents @dir", "counts", "**3 documents** in `docs/guide`"),
        ("@dir @taxonomy", "taxonomy", "docs / guide"),
    ],
)
def test_scope_keywords_narrow_the_index_lookups(
    region: dict[str, Any], message: str, action: str, check: str
) -> None:
    answer = _ask(region, message, **OPEN)

    assert answer["route"]["intent"] == "inventory"
    assert answer["inventory"]["action"] == action
    assert check in answer["answer"], answer["answer"][:300]


def test_dir_search_lists_only_the_folder(region: dict[str, Any]) -> None:
    answer = _ask(region, "@dir @search vault", **OPEN)

    cited = [c.get("relative_path") for c in answer["citations"]]
    assert answer["route"]["intent"] == "search"
    assert cited and all(path_within(p, "guide") for p in cited), cited


def test_an_unscoped_question_carries_no_scope(region: dict[str, Any]) -> None:
    assert "scope" not in _ask(region, "restart the vault", **OPEN)


# ---------------------------------------------------------------------------
# 5. Both backends
# ---------------------------------------------------------------------------

POSTGRES_DSN = os.environ.get("PHEASANT_TEST_POSTGRES_DSN", "").strip()


@pytest.mark.skipif(not POSTGRES_DSN, reason="set PHEASANT_TEST_POSTGRES_DSN to run backend parity")
def test_an_exclusion_resolves_and_applies_alike_on_both_backends(tmp_path: Path) -> None:
    """Resolution is hand-written SQL (`LOWER … LIKE … ESCAPE`, `NOT IN`)."""

    import psycopg

    with psycopg.connect(POSTGRES_DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    lite = _region(tmp_path / "sqlite")
    storage = {"backend": "postgres", "dsn_env": "PHEASANT_TEST_POSTGRES_DSN"}
    lexical = os.environ.get("PHEASANT_TEST_LEXICAL_INDEX")
    if lexical:
        storage["lexical_index"] = lexical
    pg = _region(tmp_path / "postgres", storage=storage)

    for names, within in [
        (["deploy.md"], None),
        (["DEPLOY.md", "guide/rotation.txt"], None),
        (["docs/data/table.csv"], None),
        (["deploy.md"], "guide"),
        (["table.csv"], "guide"),
    ]:
        a = scope.resolve(
            _context(lite), source_name="docs", path_prefix=within, exclude_documents=names
        )
        b = scope.resolve(
            _context(pg), source_name="docs", path_prefix=within, exclude_documents=names
        )
        assert (a.excluded, a.unresolved, a.kind) == (b.excluded, b.unresolved, b.kind), names
    for side in (lite, pg):
        found = side["tools"].search_context(
            side["kb"], QUERY, source_name="docs", exclude_documents=["rotation.txt"]
        )
        assert found["results"] and "guide/rotation.txt" not in _paths(found)
        listed = side["tools"].list_documents(
            side["kb"], source_name="docs", path_prefix="guide", exclude_documents=["deploy.md"]
        )
        assert [r["path"] for r in listed["documents"]] == [
            "guide/Setup/install.md",
            "guide/rotation.txt",
        ]
