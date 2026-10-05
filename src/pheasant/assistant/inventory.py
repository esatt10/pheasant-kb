"""Questions about the knowledge base itself, answered from the index directly.

"List all documents", "which sources are there", "how many PDFs" and "sync
status" are questions about the knowledge base, not about its content.
Retrieval cannot answer them. A search for "list all documents" returns the
eight passages that best match those three words, and a model writing from
them invents a catalogue. This module reads such a question and answers it
from ``services.inventory``, the same operation behind the MCP
``describe_knowledge_base`` / ``list_documents`` tools and HTTP
``GET /knowledge-base/overview`` / ``GET /documents``. So the chat answer is
the tool's answer, rendered.

Two ways in, both read before the history rewrite so neither costs a model
call:

* **``@pheasant``** anywhere in the question. The explicit form is
  unambiguous, so it always routes here. A request it cannot read gets the
  list of what it can, and is never sent to a search. It is the documented
  way in, and every answer from here says so. That way a reader who finds the
  automatic reading wrong, or a deployment that set
  ``assistant.inventory.mode: keyword``, still has a reliable path.
* **Rules**, when ``mode`` is ``auto`` (the default). They are deliberately
  narrow, because a false positive replaces a grounded answer with a listing.
  Every pattern is anchored to the whole question, and a qualifier the rules
  cannot resolve (for example "list the files *in the auth module*", which
  names no source) falls through to retrieval. "What is this knowledge base
  about?" is a question about content, and so is "which files mention X".
  ``tests/test_assistant_inventory.py`` holds both sides on a labelled set.

A question the rules turned down but that looks close to one gets a hint
naming ``@pheasant``. The answer text is unchanged, so retrieval baselines
stay comparable.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pheasant.ingestion.content_types import CODE_LANGUAGES

logger = logging.getLogger(__name__)

KEYWORD = "@pheasant"
MODES = ("auto", "keyword", "off")
ACTIONS = ("help", "overview", "sources", "documents", "counts", "types", "recent", "sync")
#: Documents shown for "recent" when the question names no number.
RECENT_DEFAULT = 10

_KEYWORD_RE = re.compile(r"(?<![\w@])@pheasant\b[:,]?", re.IGNORECASE)

#: Words naming a kind of document, and the extensions each one means. A
#: language name from ``CODE_LANGUAGES`` works too ("list the python files"),
#: except one-letter ones, which collide with ordinary words.
_TYPE_WORDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "pdf": ("PDF", (".pdf",)),
    "markdown": ("Markdown", (".md", ".markdown", ".mdx")),
    "md": ("Markdown", (".md", ".markdown", ".mdx")),
    "word": ("Word", (".docx", ".doc")),
    "docx": ("Word", (".docx", ".doc")),
    "powerpoint": ("PowerPoint", (".pptx",)),
    "pptx": ("PowerPoint", (".pptx",)),
    "excel": ("spreadsheet", (".xlsx", ".xls", ".csv")),
    "xlsx": ("spreadsheet", (".xlsx", ".xls", ".csv")),
    "spreadsheet": ("spreadsheet", (".xlsx", ".xls", ".csv")),
    "csv": ("CSV", (".csv",)),
    "image": ("image", (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff")),
    "text": ("text", (".txt",)),
    "html": ("HTML", (".html", ".htm")),
    "json": ("JSON", (".json",)),
    "yaml": ("YAML", (".yaml", ".yml")),
    "notebook": ("notebook", (".ipynb",)),
    "epub": ("EPUB", (".epub",)),
    "rtf": ("RTF", (".rtf",)),
    "code": ("code", tuple(sorted(CODE_LANGUAGES))),
    "source code": ("code", tuple(sorted(CODE_LANGUAGES))),
}
for _ext, _language in sorted(CODE_LANGUAGES.items()):
    if len(_language) > 1:
        label, known = _TYPE_WORDS.get(_language, (_language.capitalize(), ()))
        _TYPE_WORDS[_language] = (label, (*known, _ext))

_TYPE = "(?:" + "|".join(re.escape(w) for w in sorted(_TYPE_WORDS, key=len, reverse=True)) + ")"
# Strict names only, for patterns about the knowledge base as a whole: "what is
# in the index" is as often about a database index as about this one.
_KB_STRICT = r"(?:(?:the|this|your|my|our) )?(?:knowledge ?base|kb)"
_KB = r"(?:(?:the|this|your|my|our) )?(?:knowledge ?base|kb|index|corpus|region)"
_SRC = r"(?:data )?(?:sources?|repositories|repos|connectors)"
_DOC = r"(?:documents?|docs|files|artifacts)"
_DET = r"(?:(?:all|every|each|any) (?:of )?)?(?:(?:the|your|my|our|its) )?"
_STATE = (
    r"(?:(?:currently |already )?(?:indexed|ingested|available|stored|loaded|configured|"
    r"connected|registered|known) )?"
)
_LIST = r"(?:list|show|display|enumerate|give|get|print|return|name|what are|which are)(?: me| us)?"
_HOLD = r"(?:have|has|hold|holds|contain|contains|index|indexes|indexed|know about|got|use|uses)"
# Not "it": after an earlier turn, "what files does it touch" is about a module.
_SUBJECT = rf"(?:you|we|i|{_KB})"
_IN_KB = rf"(?: (?:in|on|inside|within|from|of) {_KB})?"


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"^{pattern}$")


_SOURCES = (
    _rx(
        rf"{_LIST} {_DET}{_STATE}{_SRC}"
        rf"(?: (?:in|of|for|on|behind|feeding|connected to|indexed by) {_KB}"
        rf"| (?:that |which )?{_SUBJECT} {_HOLD}{_IN_KB}"
        rf"| (?:that |which )?(?:are |is )?(?:configured|connected|indexed|registered|"
        rf"available|set up){_IN_KB})?"
    ),
    _rx(rf"(?:what|which) {_SRC} (?:(?:do|does|did) )?{_SUBJECT} {_HOLD}{_IN_KB}"),
    _rx(
        rf"(?:what|which) {_SRC} (?:are|is) (?:there|configured|connected|indexed|registered|"
        rf"available|set up){_IN_KB}"
    ),
    _rx(rf"(?:what|which) {_SRC} (?:are|is) (?:in|on|behind|feeding) {_KB}"),
    _rx(
        rf"where (?:does|do) (?:{_KB_STRICT}|you) (?:get|pull) (?:its|your|the) "
        r"(?:data|content|documents|information)(?: from)?"
    ),
)
_DOCUMENT_LIST = _rx(
    rf"{_LIST} {_DET}{_STATE}(?:(?P<type>{_TYPE}) )?"
    # A bare type word only in the plural: "list the pdfs" is a listing, and
    # "show me the code" is not.
    rf"(?:{_DOC}|(?P<type_noun>{_TYPE})s)(?P<tail>(?: .+)?)"
)
_DOCUMENT_QUESTION = _rx(
    rf"(?:what|which) (?:(?P<type>{_TYPE}) )?{_DOC} "
    rf"(?:(?:(?:do|does|did) )?{_SUBJECT} {_HOLD}(?: indexed)?{_IN_KB}"
    rf"|(?:are|is) (?:there|indexed|available|stored|ingested){_IN_KB}"
    rf"|(?:are|is) (?:in|on) {_KB})"
)
_COUNT = _rx(
    rf"how many (?:(?P<type>{_TYPE}) )?(?:(?P<noun>{_DOC}|{_SRC})|(?P<type_noun>{_TYPE})s)"
    rf"(?P<tail>(?: .+)?)"
)
_COUNT_TAIL = _rx(
    rf"(?: (?:are|is) there| (?:(?:do|does) )?{_SUBJECT} (?:have|has|hold|holds|contain|"
    rf"contains)(?: indexed)?| (?:are|have been) (?:indexed|ingested|stored|available)"
    rf"| (?:are|is) (?:in|on) {_KB}| (?:in|does) {_KB}(?: (?:have|hold|contain))?)?{_IN_KB}"
)
_SIZE = _rx(rf"(?:how (?:big|large) is|what is the size of) {_KB_STRICT}")
_TYPES = (
    _rx(
        rf"what (?:kinds?|types?|sorts?|formats?) of (?:{_DOC}|content|data) "
        rf"(?:(?:are|is|do|does) )?(?:there|{_SUBJECT} {_HOLD}|(?:in|on) {_KB}|indexed|stored)"
        rf"{_IN_KB}"
    ),
    _rx(
        rf"what (?:file|document|content) (?:types|formats|kinds|extensions) "
        rf"(?:(?:are|is|do|does) )?(?:there|{_SUBJECT} (?:have|has|hold|index|indexes|contain)|"
        rf"(?:in|on) {_KB}|indexed|stored){_IN_KB}"
    ),
    # "file" is required: in a code corpus "list the types" means data types.
    _rx(rf"{_LIST} {_DET}(?:file|document) (?:types|formats|extensions){_IN_KB}"),
)
_RECENT = (
    _rx(
        rf"(?:{_LIST} )?(?:the )?(?:(?P<n>\d{{1,3}}) )?(?:most )?(?:recent(?:ly)?|latest|newest|"
        rf"last|new) (?:(?P<n2>\d{{1,3}}) )?(?:(?:added|indexed|ingested|updated|modified|"
        rf"changed) )?(?:(?P<type>{_TYPE}) )?{_DOC}(?: (?:added|indexed|ingested|updated|"
        rf"modified|changed))?(?: (?:in|to|into) {_KB})?"
    ),
    _rx(
        rf"what (?:is|was|has been|have been|got) (?:recently |newly |just |last )?"
        rf"(?:added|indexed|ingested)(?: recently| lately| last)?(?: (?:to|in|into) {_KB})?"
    ),
    _rx(rf"what is new in {_KB_STRICT}"),
)
_SYNC = (
    _rx(
        rf"when (?:was|were|did) (?:{_KB_STRICT}|{_SRC}|the (?:last )?sync|you|everything) "
        r"(?:last )?(?:synced|indexed|updated|refreshed|sync|index|update|refresh)(?: last)?"
    ),
    _rx(
        rf"(?:what is |show |show me |get )?(?:the )?(?:sync|syncing|indexing|index) "
        rf"(?:status|state|progress)(?: (?:of|for) {_KB})?"
    ),
    _rx(
        rf"is (?:{_KB_STRICT}|the (?:sync|indexing|indexer)|indexing|a sync|syncing|anything) "
        r"(?:still )?(?:running|in progress|syncing|indexing|up to date|done|finished|"
        r"complete|current|fresh|stale)"
    ),
    _rx(
        rf"(?:are|is) {_DET}{_SRC} (?:healthy|up to date|failing|syncing|indexed|stale|"
        r"broken|ok|current)"
    ),
)
_OVERVIEW = (
    _rx(rf"what is (?:in|inside) {_KB_STRICT}"),
    _rx(rf"what (?:do|does) {_KB_STRICT} (?:contain|hold|have(?: in it)?)"),
    _rx(r"what do you have indexed"),
    _rx(
        rf"(?:(?:give|show|get)(?: me)? )?(?:an? |the )?(?:inventory|stats|statistics|census|"
        rf"breakdown) (?:of|for|on) {_KB_STRICT}(?:'s)?(?: (?:contents|sources|documents))?"
    ),
    _rx(rf"{_KB_STRICT}(?:'s)? (?:stats|statistics|inventory|info|information|overview|summary)"),
)
#: Close to an inventory question without being one the rules will take:
#: "list the documents about deployment", "which files cover rotation".
_WEAK = re.compile(
    rf"\b(?:list|how many|enumerate|catalog(?:ue)?|inventory)\b.*\b(?:{_DOC}|{_SRC})\b"
    rf"|\b{_KB_STRICT}\b.*\b(?:{_DOC}|{_SRC})\b"
)

#: One-word commands after ``@pheasant`` ("@pheasant sources").
_COMMANDS = {
    "help": "help",
    "commands": "help",
    "?": "help",
    "sources": "sources",
    "source": "sources",
    "repos": "sources",
    "documents": "documents",
    "docs": "documents",
    "files": "documents",
    "overview": "overview",
    "stats": "overview",
    "statistics": "overview",
    "summary": "overview",
    "info": "overview",
    "inventory": "overview",
    "count": "counts",
    "counts": "counts",
    "types": "types",
    "formats": "types",
    "extensions": "types",
    "recent": "recent",
    "latest": "recent",
    "new": "recent",
    "sync": "sync",
    "status": "sync",
    "indexing": "sync",
}


@dataclass(frozen=True)
class InventoryQuestion:
    """How a question was read as one about the knowledge base itself."""

    action: str
    trigger: str  # "keyword" or "rule"
    why: str
    source_name: str | None = None
    extensions: tuple[str, ...] = ()
    type_label: str | None = None
    path_contains: str | None = None
    limit: int | None = None
    #: Set when ``@pheasant`` arrived with a request this module could not
    #: read, so the answer can say so instead of guessing.
    unread: str | None = None
    notes: tuple[str, ...] = ()


def has_keyword(question: str) -> bool:
    return bool(_KEYWORD_RE.search(question or ""))


def read_question(
    question: str,
    *,
    mode: str = "auto",
    sources: Iterable[str] | Callable[[], Iterable[str]] = (),
) -> InventoryQuestion | None:
    """The inventory reading of ``question``, or ``None`` to answer it by retrieval.

    ``sources`` names the registered sources, so "list the documents in notes"
    can be read as a filter rather than as a phrase to search for. It may be a
    callable, which is only called once a pattern matched. Most questions never
    match, and they never pay for the lookup.
    """

    mode = str(mode or "auto").strip().lower()
    if mode == "off":
        return None
    keyword = has_keyword(question)
    if not keyword and mode != "auto":
        return None
    text = _normalize(_KEYWORD_RE.sub(" ", question or "") if keyword else question)
    trigger = "keyword" if keyword else "rule"
    resolver = _SourceResolver(sources)
    found = _read(text, keyword=keyword, sources=resolver)
    if found is None and keyword and text:
        # "@pheasant pdfs in notes" is a listing without its verb.
        found = _read(f"list {text}", keyword=True, sources=resolver)
    if found is not None:
        action, why, filters = found
        return InventoryQuestion(action=action, trigger=trigger, why=why, **filters)
    if not keyword:
        return None
    if not text:
        return InventoryQuestion(action="help", trigger=trigger, why="@pheasant on its own")
    command = _COMMANDS.get(text.split()[0])
    if command:
        rest = text.split(" ", 1)[1] if " " in text else ""
        filters = _tail_filters(rest, keyword=True, sources=resolver) or {}
        return InventoryQuestion(
            action=command, trigger=trigger, why=f"@pheasant {text.split()[0]}", **filters
        )
    return InventoryQuestion(
        action="help", trigger=trigger, why="@pheasant with a request it cannot read", unread=text
    )


def looks_close(question: str) -> bool:
    """True for a question that did not route here but reads near one."""

    return bool(_WEAK.search(_normalize(question)))


HINT = (
    "To list the knowledge base's own sources or documents rather than search "
    f"their content, start the question with {KEYWORD} (for example "
    f"`{KEYWORD} list documents`)."
)


def _normalize(text: str) -> str:
    text = " ".join((text or "").lower().replace("’", "'").split())
    text = re.sub(r"\bwhat's\b|\bwhats\b", "what is", text)
    text = re.sub(r"^(?:(?:hey|hi|hello|ok|okay|so)[,!]? )", "", text)
    text = re.sub(
        r"^(?:(?:(?:can|could|would|will) you (?:please )?|please |pls |kindly |"
        r"i (?:want|would like|'d like) to (?:see|know) |tell me ))+",
        "",
        text,
    )
    text = re.sub(r"(?:,? (?:please|for me|thanks|thank you))+[?.!\s]*$", "", text)
    return text.strip(" ?.!,:")


class _SourceResolver:
    def __init__(self, sources: Iterable[str] | Callable[[], Iterable[str]]) -> None:
        self._sources = sources
        self._names: dict[str, str] | None = None

    def find(self, name: str) -> str | None:
        if self._names is None:
            raw = self._sources() if callable(self._sources) else self._sources
            self._names = {str(n).lower(): str(n) for n in raw or ()}
        return self._names.get(name.lower())


def _types(*words: str | None) -> dict[str, Any]:
    for word in words:
        if word:
            label, extensions = _TYPE_WORDS[word]
            return {"type_label": label, "extensions": extensions}
    return {}


_TAIL_KB = _rx(
    rf"(?:(?:that |which )?(?:are |is )?(?:in|on|inside|within|from|of) {_KB}"
    rf"|(?:that |which )?{_SUBJECT} {_HOLD}(?: indexed)?{_IN_KB}"
    rf"|(?:that |which )?(?:are |have been )?(?:indexed|ingested|stored|available){_IN_KB})"
)
_TAIL_SOURCE = _rx(
    r"(?:(?:that |which )?(?:are |is )?(?:in|from|of|under|inside|within) )"
    r"(?:the )?(?:source |repo |repository |folder |collection )?(?P<name>[\w.\-/]+)"
    rf"(?: (?:source|repo|repository|folder|collection))?{_IN_KB}"
)
_TAIL_TYPE = _rx(
    rf"(?:(?:that are|of type|with (?:the )?extension|ending (?:in|with)) )"
    rf"(?:(?P<type>{_TYPE})s?|\.(?P<ext>[a-z0-9]{{1,8}}))(?: (?:files|documents))?{_IN_KB}"
)
_TAIL_PATH = _rx(r"(?:matching|containing|named|called|like|with|about|under) (?P<text>.+)")


def _tail_filters(tail: str, *, keyword: bool, sources: _SourceResolver) -> dict[str, Any] | None:
    """Filters a listing's qualifier names, or ``None`` if it names something else."""

    tail = tail.strip()
    if not tail or _TAIL_KB.match(tail):
        return {}
    typed = _TAIL_TYPE.match(tail)
    if typed:
        if typed.group("type"):
            return _types(typed.group("type"))
        ext = f".{typed.group('ext')}"
        return {"type_label": ext, "extensions": (ext,)}
    located = _TAIL_SOURCE.match(tail)
    if located:
        name = sources.find(located.group("name"))
        if name:
            return {"source_name": name}
        if keyword:
            return {
                "path_contains": located.group("name"),
                "notes": (f"no source is named “{located.group('name')}”; matched paths instead",),
            }
        # "the files in the auth module" names a part of the content, not a
        # source. Retrieval reads it better than a path filter would.
        return None
    if keyword:
        wanted = _TAIL_PATH.match(tail)
        return {"path_contains": (wanted.group("text") if wanted else tail).strip()}
    return None


def _read(
    text: str, *, keyword: bool, sources: _SourceResolver
) -> tuple[str, str, dict[str, Any]] | None:
    if not text:
        return None
    if any(p.match(text) for p in _SOURCES):
        return "sources", "asks which sources the knowledge base holds", {}
    for pattern in _RECENT:
        recent = pattern.match(text)
        if recent:
            groups = recent.groupdict()
            number = groups.get("n") or groups.get("n2")
            filters = _types(groups.get("type"))
            if number:
                filters["limit"] = max(1, int(number))
            return "recent", "asks what was indexed most recently", filters
    listed = _DOCUMENT_LIST.match(text)
    if listed:
        filters = _tail_filters(listed.group("tail"), keyword=keyword, sources=sources)
        if filters is not None:
            filters = {**_types(listed.group("type"), listed.group("type_noun")), **filters}
            return "documents", "asks for a list of the indexed documents", filters
    asked = _DOCUMENT_QUESTION.match(text)
    if asked:
        return "documents", "asks which documents are indexed", _types(asked.group("type"))
    counted = _COUNT.match(text)
    if counted:
        tail = counted.group("tail") or ""
        filters: dict[str, Any] | None = {} if _COUNT_TAIL.match(tail) else None
        if filters is None:
            filters = _tail_filters(tail, keyword=keyword, sources=sources)
        if filters is not None:
            noun = counted.group("noun") or ""
            what = "sources" if re.fullmatch(_SRC, noun) else "documents"
            filters = {**_types(counted.group("type"), counted.group("type_noun")), **filters}
            return "counts", f"asks how many {what} the knowledge base holds", filters
    if _SIZE.match(text):
        return "counts", "asks how big the knowledge base is", {}
    if any(p.match(text) for p in _TYPES):
        return "types", "asks which kinds of documents are indexed", {}
    if any(p.match(text) for p in _SYNC):
        return "sync", "asks about indexing status", {}
    if any(p.match(text) for p in _OVERVIEW):
        return "overview", "asks what the knowledge base contains", {}
    return None


def route(
    question: str, *, mode: str, state: Any, visual: str = "none"
) -> InventoryQuestion | None:
    """:func:`read_question` for the answering pipeline: never raises.

    A rule-matched question that also asked for a picture ("draw a diagram of
    the sources") is left to the workflow, which can draw. ``@pheasant`` is
    explicit and always routes here. Source names are read from ``state``
    only once a pattern has matched.
    """

    def sources() -> list[str]:
        if state is None:
            return []
        try:
            return [str(row["name"]) for row in state.rows("SELECT name FROM sources")]
        except Exception:  # a filter it cannot resolve falls through to retrieval
            logger.debug("source names unavailable for inventory routing", exc_info=True)
            return []

    try:
        asked = read_question(question, mode=mode, sources=sources)
    except Exception:  # routing must never take an answer down
        logger.exception("inventory routing failed; answering by retrieval")
        return None
    if asked is not None and asked.trigger == "rule" and visual != "none":
        return None
    return asked
