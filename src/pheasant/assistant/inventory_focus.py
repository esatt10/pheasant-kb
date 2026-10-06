"""Reading a question about one source, one document, or the links between them.

``assistant.inventory`` reads questions about the knowledge base as a whole:
the sources, the documents, how many. This module reads the narrower ones that
come next, and hands back an action for ``inventory_answer`` to look up:

* **source** — "tell me about the notes source", ``@pheasant source notes``,
  ``@pheasant notes``. A name is read as a source only if a source has it.
* **document** — "what links to deploy.md", "what does sync.py import",
  ``@pheasant document runbooks/rotation.md``, ``@pheasant deploy.md``.
* **links** — "links between notes and code", "how are the notes and code
  sources related", "cross-source links", ``@pheasant links in notes``,
  ``@pheasant imports in code``, and one document's links a page at a time:
  ``@pheasant links to deploy.md`` (its backlinks), ``links from sync.py``.
* **more** — ``@pheasant more`` / ``next``: the next page of the listing the
  conversation last asked for (``assistant.inventory.continue_from``).

The same posture as the module it extends. Without ``@pheasant``, a pattern has
to match the whole question, and every name in it has to resolve: a source
name to a registered source, and a document to something that looks like a
file (``deploy.md``, not "it"). A document the rules named but the index does
not hold is answered by retrieval, never by "not found". With ``@pheasant`` the
reading is looser and a miss is said out loud, because the reader asked for
the index explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

FindSource = Callable[[str], str | None]

#: Words naming an edge type a reader can ask for ("@pheasant imports in code").
EDGE_WORDS = {
    "import": "imports",
    "imports": "imports",
    "reference": "references",
    "references": "references",
    "embed": "embeds",
    "embeds": "embeds",
    "call": "calls",
    "calls": "calls",
}

_NAME = r"(?P<name>[\w.\-]+)"
_A = r"(?:the )?(?P<a>[\w.\-]+)(?: (?:source|repo|repository|collection|folder)s?)?"
_B = r"(?:the )?(?P<b>[\w.\-]+)(?: (?:source|repo|repository|collection|folder)s?)?"
_SRC_NOUN = r"(?:source|repo|repository|collection)"
#: Something that looks like a file: a token ending in an extension.
_FILE = r"`?(?P<path>[^\s`'\"]*[\w\-]\.[a-z0-9]{1,8})`?"
#: Anything a keyword question offers as a path: one token.
_ANY_PATH = r"`?(?P<path>[^\s`'\"]+)`?"
_DOC_NOUN = r"(?:(?:other )?(?:files|documents|docs|pages|notes) )"
_ABOUT = (
    r"(?:describe|tell me about|about|show(?: me)?|details (?:of|for|on|about)|"
    r"info(?:rmation)? (?:on|about)|overview of|status of|stats (?:for|on)|what is in)"
)
_LINK_NOUN = r"(?:links|relations(?:hips)?|connections|references|dependencies|edges)"
_RELATE = r"(?:relate|related|linked|connected|link|connect|tied together)"
#: Verbs for "X points at Y", and the direction a question with them looks.
_POINTS_AT = (
    r"(?:links? to|refers? to|references|reference|imports?|embeds?|includes?|uses?|"
    r"depends? on|points? to)"
)


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"^{pattern}$")


_SOURCE_RULES = (
    _rx(rf"(?:{_ABOUT} )?(?:the )?{_NAME} {_SRC_NOUN}"),
    _rx(rf"what (?:does|is in) (?:the )?{_NAME} {_SRC_NOUN}(?: contain| hold| have)?"),
    _rx(rf"(?:{_ABOUT} )?(?:the )?{_SRC_NOUN} (?:named|called) {_NAME}"),
)
_SOURCE_KEYWORD = (
    _rx(rf"{_SRC_NOUN}s?:? (?:named |called )?{_NAME}"),
    _rx(rf"{_ABOUT} (?:the )?{_NAME}"),
    _rx(_NAME),
)


def _document_patterns(path: str) -> tuple[tuple[re.Pattern[str], str], ...]:
    return (
        (_rx(rf"(?:what|which|who) {_DOC_NOUN}?{_POINTS_AT} {path}"), "in"),
        (
            _rx(
                rf"(?:list|show(?: me)?|find) (?:the |all )?(?:files|documents|docs|pages) "
                rf"(?:that )?(?:link(?:ing)? to|referenc(?:e|es|ing)|import(?:s|ing)?|"
                rf"embed(?:s|ding)?|us(?:e|es|ing)) {path}"
            ),
            "in",
        ),
        (_rx(rf"(?:backlinks|inbound links|incoming links) (?:to|for|of|into) {path}"), "in"),
        (_rx(rf"what (?:does|do) {path} {_POINTS_AT}"), "out"),
        (
            _rx(rf"(?:what|which) (?:files|documents|docs|pages) (?:does|do) {path} {_POINTS_AT}"),
            "out",
        ),
        (_rx(rf"(?:outbound|outgoing) links (?:from|of) {path}"), "out"),
        (_rx(rf"(?:links|relations(?:hips)?|connections) (?:of|for) {path}"), None),
    )


_DOCUMENT_RULES = _document_patterns(_FILE)
_DOCUMENT_KEYWORD = (
    *_document_patterns(_ANY_PATH),
    (_rx(rf"(?:document|doc|file|page|artifact):? {_ANY_PATH}"), None),
    (
        _rx(
            rf"(?:{_ABOUT}|open|outline (?:of|for)|structure of) "
            rf"(?:the )?(?:document |doc |file )?{_ANY_PATH}"
        ),
        None,
    ),
    (_rx(_FILE), None),
)
_LINKS_BETWEEN = (
    _rx(
        rf"(?:(?:what|which|list|show(?: me)?|find|get) )?(?:are )?(?:the |all )?{_LINK_NOUN} "
        rf"(?:are there )?(?:between|from) {_A} (?:and|to) {_B}"
    ),
    _rx(rf"how (?:is|are|do|does) {_A} and {_B} {_RELATE}(?: to each other)?"),
    _rx(rf"how (?:is|are|does|do) {_A} {_RELATE} (?:to|with) {_B}"),
)
_LINKS_ALL = (
    _rx(
        rf"(?:(?:list|show(?: me)?|what are|which are|find|get) )?(?:the |all )?"
        rf"(?:cross[- ]?source|inter[- ]?source|cross[- ]repo) {_LINK_NOUN}"
    ),
    _rx(
        rf"(?:(?:list|show(?: me)?|what are|which are|find|get) )?(?:the |all )?{_LINK_NOUN} "
        r"(?:between|across) (?:the |my |your |all )?(?:sources|repos|repositories)"
    ),
    _rx(
        rf"how (?:are|do) (?:the |my |your |all )?(?:sources|repos|repositories) {_RELATE}"
        r"(?: to each other)?"
    ),
)
_LINKS_IN = _rx(
    rf"(?:(?:list|show(?: me)?|what are|which are|find|get) )?(?:the |all )?{_LINK_NOUN} "
    rf"(?:in|of|for|from|to|into|within|touching) {_A}"
)
_LINKS_COMMAND = _rx(
    r"(?P<noun>links|link|relations|relationships|connections|graph|edges|imports|references|"
    r"embeds|calls)(?P<tail>(?: .+)?)"
)
_MORE = _rx(
    r"(?:more|next|next page|continue|show more|load more|the rest|keep going|"
    r"more results|next results)"
)


def read_focus(
    text: str, *, keyword: bool, find_source: FindSource, reserved: Any = ()
) -> tuple[str, str, dict[str, Any]] | None:
    """``(action, why, filters)`` for a source, document or links question, or ``None``.

    ``reserved`` are words that are commands on their own (``@pheasant sync``),
    so a source that happens to share one is not read in its place.
    """

    if keyword and _MORE.match(text):
        return "more", "asks for the next page", {}
    links = _read_links(text, keyword=keyword, find_source=find_source)
    if links is not None:
        return links
    for pattern in _SOURCE_RULES + (_SOURCE_KEYWORD if keyword else ()):
        matched = pattern.match(text)
        if matched and text in reserved:
            continue
        if matched:
            name = find_source(matched.group("name"))
            if name:
                return "source", f"asks about the source “{name}”", {"source_name": name}
    patterns = _DOCUMENT_KEYWORD if keyword else _DOCUMENT_RULES
    for pattern, direction in patterns:
        matched = pattern.match(text)
        if not matched:
            continue
        path = matched.group("path").strip("`")
        if not keyword and find_source(path):
            continue
        why = {
            "in": "asks what links to a document",
            "out": "asks what a document links to",
        }.get(str(direction), "asks about one document")
        return "document", why, {"path": path, "direction": direction}
    return None


def _read_links(
    text: str, *, keyword: bool, find_source: FindSource
) -> tuple[str, str, dict[str, Any]] | None:
    for pattern in _LINKS_BETWEEN:
        matched = pattern.match(text)
        if matched:
            a, b = find_source(matched.group("a")), find_source(matched.group("b"))
            if a and b:
                return (
                    "links",
                    f"asks how “{a}” and “{b}” are linked",
                    {"source_name": a, "other_source": b},
                )
    if any(pattern.match(text) for pattern in _LINKS_ALL):
        return "links", "asks for links across sources", {"cross_source_only": True}
    matched = _LINKS_IN.match(text)
    if matched:
        name = find_source(matched.group("a"))
        if name:
            return "links", f"asks for the links of “{name}”", {"source_name": name}
    if not keyword:
        return None
    command = _LINKS_COMMAND.match(text)
    if command is None:
        return None
    filters: dict[str, Any] = {}
    noun = command.group("noun")
    if noun in EDGE_WORDS:
        filters["edge_types"] = (EDGE_WORDS[noun],)
    tail = (command.group("tail") or "").strip()
    if not tail:
        return "links", "asks for the links between documents", filters
    read = _read_links(f"links {tail}", keyword=False, find_source=find_source)
    if read is not None:
        return read[0], read[1], {**read[2], **filters}
    name = find_source(tail.removeprefix("the ").split(" ")[0]) if tail else None
    if name and len(tail.removeprefix("the ").split(" ")) == 1:
        return "links", f"asks for the links of “{name}”", {**filters, "source_name": name}
    toward = re.fullmatch(rf"(?P<way>to|into|from|out of) {_ANY_PATH}", tail)
    if toward:
        direction = "out" if toward.group("way") in {"from", "out of"} else "in"
        return (
            "links",
            "asks for one document's links, a page at a time",
            {**filters, "path": toward.group("path"), "direction": direction},
        )
    if re.fullmatch(r"(?:cross[- ]?source|across sources|between sources)", tail):
        return "links", "asks for links across sources", {**filters, "cross_source_only": True}
    path = re.fullmatch(rf"(?:of |for |to |from )?{_ANY_PATH}", tail)
    if path and not filters:
        return (
            "document",
            "asks about one document's links",
            {"path": path.group("path"), "direction": None},
        )
    return None
