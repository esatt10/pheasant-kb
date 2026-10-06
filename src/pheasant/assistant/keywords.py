"""First-word keywords: the reader says what kind of answer they want.

The routers in ``assistant.routing`` read a question's *words* for its length
and whether it wants a picture, and ``assistant.inventory`` reads whether it is
about the knowledge base itself. Rules are right most of the time and cost
nothing, and a reader who knows what they want should not have to phrase the
question so a rule notices. A keyword says it directly:

* **Shape** of a written answer: ``@table``, ``@list``, ``@steps``,
  ``@compare``, ``@quotes``, ``@brief``. Each adds one FORMAT instruction to
  the answering prompt (:data:`FORM_INSTRUCTIONS`). The grounding rules are
  unchanged, so every cell, bullet and step still cites its passage.
* **Length**: ``@overview`` (medium) and ``@detailed`` (long), the same pins
  the request's ``depth`` takes.
* **Pictures**: ``@diagram`` and any diagram shape by name
  (``@timeline``, ``@flow``, ``@mindmap`` ...), the same pins as ``visual``.
* **Deterministic answers**, with no model in the path: ``@search`` (the
  ranked hybrid-search hits, as a table), and the index lookups ``@source``,
  ``@doc``, ``@docs``, ``@links`` and ``@more``, which are shorthand for
  ``@pheasant source …``, ``@pheasant document …`` and so on.

A keyword counts only as the **first word** of the message (several may lead
it: ``@detailed @table …``), so a question that merely mentions one, or an
email address, is never read as one. ``@pheasant`` is the exception and may
appear anywhere, as it always could. A leading ``@word`` that is no keyword is
left in the question untouched and reported as ``unknown``, so a mistyped
keyword is visible rather than silently searched for.

A keyword wins over the request's own pins (the UI's length selector, say): it
was typed into this message, and the selector is a standing preference.
Everything here is a pure function of the text. Nothing calls a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pheasant.assistant.visual_specs import KINDS, normalize_kind

INVENTORY_KEYWORD = "pheasant"


@dataclass(frozen=True)
class Keyword:
    """One first-word keyword and what it asks for."""

    name: str
    #: ``inventory`` | ``search`` | ``form`` | ``depth`` | ``visual``
    kind: str
    value: str
    summary: str
    example: str
    aliases: tuple[str, ...] = ()
    #: The group it is listed under in help.
    group: str = ""


KEYWORDS: tuple[Keyword, ...] = (
    Keyword(
        INVENTORY_KEYWORD,
        "inventory",
        "",
        "Ask about the knowledge base itself: sources, documents, links, sync. Anywhere "
        "in the message.",
        "@pheasant list sources",
        group="Answered without a model",
    ),
    Keyword(
        "source",
        "inventory",
        "source",
        "One source in detail: what it holds, by type and folder, and what it links to.",
        "@source notes",
        ("repo",),
        group="Answered without a model",
    ),
    Keyword(
        "doc",
        "inventory",
        "document",
        "One document: outline, symbols, the documents it links to and is linked from.",
        "@doc runbooks/rotation.md",
        ("document", "file"),
        group="Answered without a model",
    ),
    Keyword(
        "docs",
        "inventory",
        "documents",
        "List documents, filtered and paged (add `page 2`).",
        "@docs pdfs in notes",
        ("documents", "files"),
        group="Answered without a model",
    ),
    Keyword(
        "links",
        "inventory",
        "links",
        "Links between documents, per source pair and edge type.",
        "@links between notes and code",
        ("relations", "graph"),
        group="Answered without a model",
    ),
    Keyword(
        "more",
        "inventory",
        "more",
        "The next page of the last listing in this conversation.",
        "@more",
        ("next",),
        group="Answered without a model",
    ),
    Keyword(
        "search",
        "search",
        "",
        "The ranked hybrid-search hits as a table, with no model writing an answer.",
        "@search credential rotation",
        ("find",),
        group="Answered without a model",
    ),
    Keyword(
        "brief",
        "form",
        "brief",
        "At most three sentences.",
        "@brief how does rotation work",
        ("tldr", "short"),
        group="Shape of the answer",
    ),
    Keyword(
        "table",
        "form",
        "table",
        "A Markdown table, a citation in each cell.",
        "@table the services and their ports",
        group="Shape of the answer",
    ),
    Keyword(
        "list",
        "form",
        "list",
        "A bulleted list, one cited point per bullet.",
        "@list the deployment prerequisites",
        ("bullets",),
        group="Shape of the answer",
    ),
    Keyword(
        "steps",
        "form",
        "steps",
        "Numbered steps in order, commands verbatim.",
        "@steps rotate the gateway credentials",
        ("howto",),
        group="Shape of the answer",
    ),
    Keyword(
        "compare",
        "form",
        "compare",
        "A comparison table, one column per thing compared.",
        "@compare text and vector search",
        ("vs",),
        group="Shape of the answer",
    ),
    Keyword(
        "quotes",
        "form",
        "quotes",
        "Verbatim quotes from the sources, each cited.",
        "@quotes what the runbook says about downtime",
        ("quote",),
        group="Shape of the answer",
    ),
    Keyword(
        "overview",
        "depth",
        "medium",
        "A medium-length answer in a few headed sections.",
        "@overview the retrieval pipeline",
        ("medium",),
        group="Length",
    ),
    Keyword(
        "detailed",
        "depth",
        "long",
        "A long, outlined write-up, section by section.",
        "@detailed how indexing works",
        ("long", "report", "deep"),
        group="Length",
    ),
    Keyword(
        "diagram",
        "visual",
        "diagram",
        "A grounded diagram in whichever shape fits; name one (`@timeline`, `@flow`, "
        "`@mindmap`, `@sequence`, `@hierarchy` …) to choose it.",
        "@diagram the sync pipeline",
        ("draw", "visual"),
        group="Pictures",
    ),
)

#: Every name and alias a keyword answers to, without the ``@``.
_BY_NAME: dict[str, Keyword] = {}
for _keyword in KEYWORDS:
    for _name in (_keyword.name, *_keyword.aliases):
        _BY_NAME[_name] = _keyword
#: A diagram shape by name is a keyword too (``@timeline``), except ``table``,
#: which is the written answer's shape: a table that reads in any client.
for _kind in KINDS:
    if _kind != "table" and _kind not in _BY_NAME:
        _BY_NAME[_kind] = Keyword(_kind, "visual", _kind, f"A {_kind} diagram.", f"@{_kind}")

#: The FORMAT instruction each answer shape appends to the answering prompt.
FORM_INSTRUCTIONS = {
    "brief": """

FORMAT: at most three sentences. No headings, no lists. Cite as usual.""",
    "table": """

FORMAT: answer with a Markdown table. Choose the columns the question calls \
for (for example item, what it is, where it is defined). Put each citation \
marker inside the cell it supports. At most one sentence before the table and \
one after it. If the passages hold nothing tabular, say so in one sentence \
instead of inventing rows.""",
    "list": """

FORMAT: answer with a bulleted Markdown list, one point per bullet, each \
bullet ending with the citation for that point. No paragraphs before or after \
beyond one short lead-in line.""",
    "steps": """

FORMAT: answer with numbered steps, in the order they are done. One action per \
step, each citing its passage. Copy commands, paths and code from the passages \
verbatim into fenced code blocks. Never invent a step the passages do not \
state; if they stop short, say where.""",
    "compare": """

FORMAT: a comparison. A Markdown table with one column per thing compared and \
one row per aspect, citations in the cells; then one sentence naming the \
difference that matters most. Leave a cell as "not stated" rather than guess.""",
    "quotes": """

FORMAT: answer with verbatim quotes from the passages. Each quote is a \
Markdown blockquote (`> …`) copied exactly, followed by its citation marker on \
the line after. At most one sentence of framing per quote. Never paraphrase \
inside a quote, and never quote text the passages do not contain.""",
}

_LEADING = re.compile(r"^\s*@(?P<name>[a-z][\w-]*)\b[:,]?\s*", re.IGNORECASE)


@dataclass(frozen=True)
class Directives:
    """What a message's leading keywords asked for, and the question without them."""

    text: str
    used: tuple[str, ...] = ()
    #: A leading ``@word`` that is no keyword, left in ``text``.
    unknown: str | None = None
    #: An index lookup's ``@pheasant`` command (``source``, ``document`` …),
    #: or ``""`` when the message leads with ``@pheasant`` itself.
    inventory: str | None = None
    search: bool = False
    form: str | None = None
    depth: str | None = None
    visual: str | None = None
    why: dict[str, str] = field(default_factory=dict)

    @property
    def any(self) -> bool:
        return bool(self.used)

    def as_dict(self) -> dict[str, object]:
        return {
            "used": list(self.used),
            "unknown": self.unknown,
            "form": self.form,
            "depth": self.depth,
            "visual": self.visual,
            "search": self.search,
        }


def read(question: str) -> Directives:
    """The keywords leading ``question``. Pure; never raises."""

    text = question or ""
    used: list[str] = []
    found: dict[str, object] = {}
    unknown = None
    while True:
        matched = _LEADING.match(text)
        if not matched:
            break
        name = matched.group("name").lower()
        if name == INVENTORY_KEYWORD:
            # Left in the text: `assistant.inventory` reads it wherever it is.
            found.setdefault("inventory", "")
            break
        keyword = _BY_NAME.get(name) or _shape(name)
        if keyword is None:
            unknown = f"@{name}"
            break
        used.append(f"@{keyword.name}")
        text = text[matched.end() :]
        if keyword.kind == "inventory":
            found["inventory"] = keyword.value
            break
        if keyword.kind == "search":
            found["search"] = True
            break
        if keyword.kind == "form":
            found["form"] = keyword.value
            if keyword.value == "brief":
                found["depth"] = "short"
        elif keyword.kind == "depth":
            found["depth"] = keyword.value
        elif keyword.kind == "visual":
            found["visual"] = keyword.value
    return Directives(
        text=text.strip(),
        used=tuple(used),
        unknown=unknown,
        inventory=found.get("inventory"),  # type: ignore[arg-type]
        search=bool(found.get("search")),
        form=found.get("form"),  # type: ignore[arg-type]
        depth=found.get("depth"),  # type: ignore[arg-type]
        visual=found.get("visual"),  # type: ignore[arg-type]
    )


def inventory_form(question: str) -> str:
    """``question`` with a leading index keyword spelled as ``@pheasant``.

    ``@doc deploy.md`` is ``@pheasant document deploy.md``; anything else is
    returned unchanged. ``assistant.inventory`` reads the result, which keeps
    one reader for both spellings, history included.
    """

    directives = read(question)
    if directives.inventory:
        return f"@{INVENTORY_KEYWORD} {directives.inventory} {directives.text}".strip()
    return question or ""


def form_instruction(form: str | None) -> str:
    return FORM_INSTRUCTIONS.get(str(form or ""), "")


def help_rows() -> list[tuple[str, list[Keyword]]]:
    """The keywords grouped for a help answer, in declaration order."""

    groups: dict[str, list[Keyword]] = {}
    for keyword in KEYWORDS:
        groups.setdefault(keyword.group, []).append(keyword)
    return list(groups.items())


def catalog() -> list[dict[str, object]]:
    """Every keyword, for a client that offers them as you type."""

    return [
        {
            "keyword": f"@{keyword.name}",
            "aliases": [f"@{alias}" for alias in keyword.aliases],
            "kind": keyword.kind,
            "group": keyword.group,
            "summary": keyword.summary,
            "example": keyword.example,
            "anywhere": keyword.name == INVENTORY_KEYWORD,
        }
        for keyword in KEYWORDS
    ]


def keyword_catalog(settings: object) -> list[dict[str, object]]:
    """:func:`catalog`, less what ``assistant`` settings turned off."""

    inventory_on = str(getattr(getattr(settings, "inventory", None), "mode", "auto")) != "off"
    first_words = getattr(settings, "keywords", True) is not False
    return [
        row
        for row in catalog()
        if (row["kind"] != "inventory" or inventory_on) and (row["anywhere"] or first_words)
    ]


def _shape(name: str) -> Keyword | None:
    kind = normalize_kind(name)
    if kind and kind != "table":
        return Keyword(kind, "visual", kind, f"A {kind} diagram.", f"@{kind}")
    return None
