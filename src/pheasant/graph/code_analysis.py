"""Symbols, imports and calls for the languages ``ast`` cannot read.

Python is analysed in :mod:`pheasant.graph.enrichment` with the standard
library's ``ast``. Every other language in
:data:`pheasant.ingestion.content_types.CODE_LANGUAGES` comes through here:
deterministic patterns over text that :func:`~pheasant.graph.code_scan.mask`
has already stripped of comments and strings. The output is the same three
things the Python pass produces, so the graph and the tools that walk it need
not know which reader produced a node:

* **imports**, as the specifier the source wrote, typed per language family
  (``js_import``, ``go_import``, ...). :mod:`pheasant.graph.code_imports`
  turns the ones that name a file into file -> file edges.
* **symbols**: ``function``, ``class`` (struct, interface, trait, enum,
  module, protocol all fold into it, as Python's do) and ``constant``, with the
  lines they span.
* **calls**, as the possibly-qualified name before ``(``.

What this is not: a compiler. A call through a variable, a macro, a method
resolved by type, a Haskell application without parentheses are invisible to
it. It errs towards missing an edge rather than inventing one, which is the
right direction for a graph that answers "what does this file touch".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pheasant.graph import code_scan as scan
from pheasant.ingestion.content_types import CODE_LANGUAGES


@dataclass(frozen=True)
class CodeImport:
    spec: str
    reference_type: str
    line: int


@dataclass(frozen=True)
class CodeSymbol:
    name: str
    kind: str  # function | class | constant
    start_line: int
    end_line: int


@dataclass(frozen=True)
class CodeCall:
    name: str
    line: int


@dataclass
class CodeAnalysis:
    language: str
    imports: list[CodeImport] = field(default_factory=list)
    symbols: list[CodeSymbol] = field(default_factory=list)
    calls: list[CodeCall] = field(default_factory=list)


@dataclass(frozen=True)
class _Lang:
    syntax: scan.Syntax
    #: (pattern over comment-masked text, reference_type). Group 1 is the spec.
    imports: tuple[tuple[re.Pattern[str], str], ...]
    #: (pattern over fully masked text, kind). The named group ``name`` is
    #: the symbol; the match end is where its body is searched for.
    symbols: tuple[tuple[re.Pattern[str], str], ...]
    blocks: str = "brace"  # brace | indent | paren
    call_separators: tuple[str, ...] = (".",)
    keywords: frozenset[str] = frozenset()
    calls: bool = True
    #: A type before a name declares it (C, Java, C#, Dart); see
    #: :func:`_declared_here`.
    typed_declarations: bool = False


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.MULTILINE)


_CONTROL = frozenset(
    "if else elif for foreach while do switch case catch try finally return "
    "throw throws new delete sizeof typeof instanceof await yield assert "
    "defer go select match when loop in is as and or not import async function "
    "func fn".split()
)
_JVM_TYPES = r"(?:class|interface|enum|record|object|trait)"

_JS_SYMBOLS = (
    (_p(r"\bfunction\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*\("), "function"),
    (_p(r"\bclass\s+(?P<name>[A-Za-z_$][\w$]*)"), "class"),
    (_p(r"\b(?:interface|enum)\s+(?P<name>[A-Za-z_$][\w$]*)"), "class"),
    (_p(r"^\s*(?:export\s+)?type\s+(?P<name>[A-Z][\w$]*)\s*(?:<[^=]{0,300}>)?\s*="), "class"),
    (
        _p(
            r"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*(?::[^=\n]{1,200})?=\s*"
            r"(?:async\s+)?(?:function\b|\([^()]{0,500}\)\s*(?::[^=\n]{1,200})?=>"
            r"|[A-Za-z_$][\w$]*\s*=>)"
        ),
        "function",
    ),
    (_p(r"\bconst\s+(?P<name>[A-Z][A-Z0-9_]+)\s*(?::[^=\n]{1,200})?="), "constant"),
    (
        _p(
            r"^[ \t]+(?:(?:public|private|protected|static|async|get|set|readonly)\s+)*"
            r"(?P<name>[A-Za-z_$][\w$]*)\s*\([^()]{0,500}\)\s*(?::[^{;\n]{1,200})?\{"
        ),
        "function",
    ),
)
_JS_IMPORTS = (
    (
        _p(r"\bimport\s+(?:type\s+)?(?:[\w$*{}\s,]{1,2000}?\s+from\s+)?[\"']([^\"'\n]+)[\"']"),
        "js_import",
    ),
    (_p(r"\bexport\s+[^;\"']*?\bfrom\s+[\"']([^\"']+)[\"']"), "js_import"),
    (_p(r"\brequire\s*\(\s*[\"']([^\"']+)[\"']\s*\)"), "js_import"),
    (_p(r"\bimport\s*\(\s*[\"']([^\"']+)[\"']\s*\)"), "js_import"),
)
_JVM_IMPORTS = ((_p(r"^\s*import\s+(?:static\s+)?([\w.]+[\w*])"), "jvm_import"),)
_C_IMPORTS = (
    (_p(r"^\s*#\s*(?:include|import)\s*\"([^\"]+)\""), "c_include"),
    (_p(r"^\s*#\s*(?:include|import)\s*<([^>]+)>"), "c_system_include"),
)
_C_SYMBOLS = (
    (
        _p(r"\b(?:struct|union|enum|class)\s+(?P<name>[A-Za-z_]\w*)\s*(?::[^{;()]{0,500})?\{"),
        "class",
    ),
    (_p(r"^\s*#\s*define\s+(?P<name>[A-Z][A-Z0-9_]+)\b"), "constant"),
    (
        _p(
            r"^[A-Za-z_][\w \t*&:<>,~]{0,200}?\b(?P<name>~?[A-Za-z_]\w*)[ \t]*"
            r"\([^;{}]{0,1000}\)\s*(?:const\s*)?(?:noexcept\s*)?(?:override\s*)?(?=\{)"
        ),
        "function",
    ),
    (
        _p(
            r"^[ \t]+(?:(?:virtual|static|inline|explicit)[ \t]+)*[A-Za-z_][\w \t*&:<>,]{0,200}?"
            r"\b(?P<name>~?[A-Za-z_]\w*)[ \t]*\([^;{}]{0,1000}\)\s*(?:const\s*)?"
            r"(?:override\s*)?(?=\{)"
        ),
        "function",
    ),
)
#: Every repeat that can cross a line is bounded: an unbounded one is
#: quadratic on a file that never closes the construct (a generated file, a
#: minified bundle), and this runs on the indexing path.
_BRACE_METHOD = (
    r"^[ \t]*(?:@\w+(?:\([^)\n]{0,200}\))?\s+)*(?:(?:public|private|protected|internal|"
    r"static|final|abstract|synchronized|native|virtual|override|async|sealed|partial|extern|"
    r"unsafe|new|readonly)[ \t]+)*[\w<>\[\],.?]{1,200}(?:[ \t]*<[^>\n]{0,200}>)?[ \t]+"
    r"(?P<name>[A-Za-z_]\w*)[ \t]*(?:<[^>\n]{0,200}>)?[ \t]*\([^;{}]{0,1000}\)\s*"
    r"(?:throws[ \t]+[\w., \t]{1,200})?(?:where[^{\n]{0,200})?\s*(?=\{)"
)

_JS_KEYWORDS = frozenset({"require", "super"})

LANGUAGES: dict[str, _Lang] = {
    "javascript": _Lang(scan.JS, _JS_IMPORTS, _JS_SYMBOLS, keywords=_JS_KEYWORDS),
    "typescript": _Lang(scan.JS, _JS_IMPORTS, _JS_SYMBOLS, keywords=_JS_KEYWORDS),
    "go": _Lang(
        scan.GO,
        (
            (_p(r"^\s*import\s+(?:[\w.]+\s+)?\"([^\"]+)\""), "go_import"),
            (_p(r"^\s*(?:[\w.]+\s+)?\"([^\"]+)\"\s*$"), "go_import_block"),
        ),
        (
            (_p(r"^func\s+(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_]\w*)"), "function"),
            (_p(r"^type\s+(?P<name>[A-Za-z_]\w*)\s+(?:struct|interface)\b"), "class"),
            (_p(r"^const\s+(?P<name>[A-Za-z_]\w*)\s*(?:[\w.\[\]*]+\s*)?="), "constant"),
        ),
        keywords=frozenset({"func", "make", "len", "cap", "append", "panic", "range"}),
    ),
    "rust": _Lang(
        scan.RUST,
        (
            (_p(r"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+(\w+)\s*;"), "rust_mod"),
            (_p(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+([\w:]+)"), "rust_use"),
            (_p(r"^\s*extern\s+crate\s+(\w+)"), "rust_use"),
        ),
        (
            (
                _p(
                    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?"
                    r"(?:extern\s+\"\w+\"\s+)?fn\s+(?P<name>[A-Za-z_]\w*)"
                ),
                "function",
            ),
            (
                _p(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+(?P<name>\w+)"),
                "class",
            ),
            (
                _p(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const|static)\s+(?P<name>[A-Z][A-Z0-9_]+)"),
                "constant",
            ),
        ),
        call_separators=("::", "."),
        keywords=frozenset({"fn", "impl", "where", "Some", "Ok", "Err"}),
    ),
    "java": _Lang(
        scan.TRIPLE,
        _JVM_IMPORTS,
        (
            (_p(rf"\b{_JVM_TYPES}\s+(?P<name>[A-Za-z_]\w*)"), "class"),
            (_p(r"\bstatic\s+final\s+[\w<>\[\]]+\s+(?P<name>[A-Z][A-Z0-9_]+)\s*="), "constant"),
            (_p(_BRACE_METHOD), "function"),
        ),
        typed_declarations=True,
    ),
    "kotlin": _Lang(
        scan.TRIPLE,
        _JVM_IMPORTS,
        (
            (_p(rf"\b{_JVM_TYPES}\s+(?P<name>[A-Za-z_]\w*)"), "class"),
            (_p(r"\bfun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(?P<name>[A-Za-z_]\w*)\s*\("), "function"),
            (_p(r"\bconst\s+val\s+(?P<name>[A-Z][A-Z0-9_]+)"), "constant"),
        ),
    ),
    "scala": _Lang(
        scan.TRIPLE,
        _JVM_IMPORTS,
        (
            (_p(rf"\b{_JVM_TYPES}\s+(?P<name>[A-Za-z_]\w*)"), "class"),
            (_p(r"\bdef\s+(?P<name>[A-Za-z_]\w*)"), "function"),
        ),
    ),
    "groovy": _Lang(
        scan.TRIPLE,
        _JVM_IMPORTS,
        (
            (_p(rf"\b{_JVM_TYPES}\s+(?P<name>[A-Za-z_]\w*)"), "class"),
            (_p(r"\bdef\s+(?P<name>[A-Za-z_]\w*)\s*\("), "function"),
            (_p(_BRACE_METHOD), "function"),
        ),
    ),
    "c": _Lang(
        scan.C_LIKE,
        _C_IMPORTS,
        _C_SYMBOLS,
        call_separators=(".", "->"),
        typed_declarations=True,
    ),
    "cpp": _Lang(
        scan.C_LIKE,
        _C_IMPORTS,
        _C_SYMBOLS,
        call_separators=("::", ".", "->"),
        typed_declarations=True,
    ),
    "objc": _Lang(
        scan.C_LIKE,
        _C_IMPORTS,
        (*_C_SYMBOLS, (_p(r"^@(?:interface|implementation|protocol)\s+(?P<name>\w+)"), "class")),
        call_separators=(".", "->"),
        typed_declarations=True,
    ),
    "csharp": _Lang(
        scan.CSHARP,
        (
            (
                _p(r"^\s*(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?([\w.]+)\s*;"),
                "csharp_using",
            ),
        ),
        (
            (_p(r"\b(?:class|interface|enum|struct|record)\s+(?P<name>[A-Za-z_]\w*)"), "class"),
            (_p(r"\bconst\s+[\w<>]+\s+(?P<name>[A-Z][A-Za-z0-9_]*)\s*="), "constant"),
            (_p(_BRACE_METHOD), "function"),
        ),
        typed_declarations=True,
    ),
    "ruby": _Lang(
        scan.HASH,
        (
            (_p(r"^\s*require_relative\s*\(?\s*[\"']([^\"']+)[\"']"), "ruby_require_relative"),
            (_p(r"^\s*(?:require|load)\s*\(?\s*[\"']([^\"']+)[\"']"), "ruby_require"),
        ),
        (
            (_p(r"^\s*(?:class|module)\s+(?P<name>[A-Z]\w*(?:::\w+)*)"), "class"),
            (_p(r"^\s*def\s+(?:self\.)?(?P<name>[A-Za-z_]\w*[?!=]?)"), "function"),
            (_p(r"^\s*(?P<name>[A-Z][A-Z0-9_]+)\s*=[^=]"), "constant"),
        ),
        blocks="indent",
        call_separators=(".", "::"),
        keywords=frozenset({"def", "puts", "require", "require_relative", "attr_accessor"}),
    ),
    "php": _Lang(
        scan.PHP,
        (
            (_p(r"^\s*(?:require|include)(?:_once)?\s*\(?\s*[\"']([^\"']+)[\"']"), "php_include"),
            (_p(r"^\s*use\s+(?:function\s+|const\s+)?([\w\\]+)"), "php_use"),
        ),
        (
            (_p(r"\b(?:class|interface|trait|enum)\s+(?P<name>[A-Za-z_]\w*)"), "class"),
            (_p(r"\bfunction\s+&?(?P<name>[A-Za-z_]\w*)\s*\("), "function"),
            (_p(r"\bconst\s+(?P<name>[A-Z][A-Z0-9_]+)\s*="), "constant"),
        ),
        call_separators=("::", "->"),
        keywords=frozenset({"function", "array", "isset", "unset", "empty", "list", "echo"}),
    ),
    "swift": _Lang(
        scan.TRIPLE,
        (
            (
                _p(
                    r"^[ \t]*(?:@testable[ \t]+)?import[ \t]+(?:(?:typealias|struct|class|"
                    r"enum|protocol|let|var|func)[ \t]+)?([\w.]+)"
                ),
                "swift_import",
            ),
        ),
        (
            (
                _p(r"\b(?:class|struct|enum|protocol|extension|actor)\s+(?P<name>[A-Za-z_]\w*)"),
                "class",
            ),
            (_p(r"\bfunc\s+(?P<name>[A-Za-z_]\w*)"), "function"),
        ),
    ),
    "dart": _Lang(
        scan.DART,
        ((_p(r"^\s*(?:import|export|part)\s+[\"']([^\"']+)[\"']"), "dart_import"),),
        (
            (_p(r"\b(?:class|mixin|enum|extension)\s+(?P<name>[A-Za-z_]\w*)"), "class"),
            (_p(_BRACE_METHOD), "function"),
            (
                _p(
                    r"^(?:[\w<>?]{1,200}[ \t]+)?(?P<name>[a-z_]\w*)[ \t]*\([^;{}]{0,1000}\)\s*"
                    r"(?:async\s*)?(?=\{)"
                ),
                "function",
            ),
        ),
        typed_declarations=True,
    ),
    "zig": _Lang(
        scan.ZIG,
        ((_p(r"@import\s*\(\s*\"([^\"]+)\"\s*\)"), "zig_import"),),
        (
            (_p(r"\b(?:pub\s+)?(?:export\s+)?fn\s+(?P<name>[A-Za-z_]\w*)"), "function"),
            (
                _p(
                    r"\b(?:pub\s+)?const\s+(?P<name>[A-Za-z_]\w*)\s*=\s*"
                    r"(?:extern\s+|packed\s+)?(?:struct|enum|union)\b"
                ),
                "class",
            ),
        ),
        keywords=frozenset({"fn"}),
    ),
    "haskell": _Lang(
        scan.HASKELL,
        ((_p(r"^import\s+(?:qualified\s+)?([A-Z][\w.]*)"), "haskell_import"),),
        (
            (_p(r"^(?:data|newtype|type|class)\s+(?P<name>[A-Z]\w*)"), "class"),
            (_p(r"^(?P<name>[a-z_][\w']*)\s*::"), "function"),
        ),
        blocks="indent",
        calls=False,
    ),
    "elixir": _Lang(
        scan.ELIXIR,
        ((_p(r"^\s*(?:alias|import|require|use)\s+([A-Z][\w.]*)"), "elixir_module"),),
        (
            (_p(r"^\s*defmodule\s+(?P<name>[A-Z][\w.]*)"), "class"),
            (_p(r"^\s*(?:defp?|defmacrop?)\s+(?P<name>[a-z_]\w*[?!]?)"), "function"),
        ),
        blocks="indent",
        keywords=frozenset({"def", "defp", "defmodule", "defmacro", "fn", "do", "end"}),
    ),
    "erlang": _Lang(
        scan.ERLANG,
        (
            (_p(r"^-include(?:_lib)?\s*\(\s*\"([^\"]+)\""), "erlang_include"),
            (_p(r"^-import\s*\(\s*(\w+)"), "erlang_module"),
        ),
        (
            (_p(r"^-record\s*\(\s*(?P<name>\w+)"), "class"),
            (_p(r"^(?P<name>[a-z]\w*)\s*\("), "function"),
        ),
        blocks="indent",
        call_separators=(":",),
        keywords=frozenset(
            {
                "fun",
                "receive",
                "after",
                "of",
                "end",
                "begin",
                "module",
                "include",
                "include_lib",
                "import",
                "export",
                "record",
                "define",
                "spec",
                "type",
            }
        ),  # fmt: skip
    ),
    "clojure": _Lang(
        scan.LISP,
        (
            (_p(r"\(\s*:require\s+\[?\s*([a-z][\w.\-]*)"), "clojure_ns"),
            (_p(r"(?<=\s)\[\s*([a-z][\w\-]*(?:\.[\w\-]+)+)"), "clojure_ns"),
        ),
        (
            (
                _p(
                    r"\(\s*(?:defprotocol|defrecord|deftype|defmulti)\s+"
                    r"(?P<name>[^\s()\[\]{}]+)"
                ),
                "class",
            ),
            (_p(r"\(\s*(?:defn-?|defmacro)\s+(?P<name>[^\s()\[\]{}]+)"), "function"),
        ),
        blocks="paren",
    ),
    "lua": _Lang(
        scan.LUA,
        ((_p(r"\brequire\s*\(?\s*[\"']([^\"']+)[\"']"), "lua_require"),),
        ((_p(r"\bfunction\s+(?P<name>[A-Za-z_][\w.:]*)\s*\("), "function"),),
        blocks="indent",
        call_separators=(".", ":"),
        keywords=frozenset({"function", "require", "local", "then", "end"}),
    ),
    "shell": _Lang(
        scan.SHELL,
        ((_p(r"^\s*(?:source|\.)\s+[\"']?([^\s\"';]+)"), "shell_source"),),
        (
            (_p(r"^\s*function\s+(?P<name>[A-Za-z_][\w-]*)"), "function"),
            (_p(r"^\s*(?P<name>[A-Za-z_][\w-]*)\s*\(\)\s*\{"), "function"),
        ),
        calls=False,
    ),
    "protobuf": _Lang(
        scan.C_LIKE,
        ((_p(r"^\s*import\s+(?:public\s+|weak\s+)?\"([^\"]+)\""), "proto_import"),),
        (
            (_p(r"^\s*(?:message|enum|service)\s+(?P<name>\w+)"), "class"),
            (_p(r"^\s*rpc\s+(?P<name>\w+)"), "function"),
        ),
        calls=False,
    ),
}

#: Clojure's special forms read as calls at the head of a list and are not.
_LISP_SPECIAL = frozenset(
    "def defn defn- defmacro fn let letfn if if-not when when-not do ns cond case "
    "loop recur try catch finally quote var throw new set! and or not".split()
)


def language_for(relative_path: str) -> str | None:
    return CODE_LANGUAGES.get(Path(relative_path).suffix.lower())


def analyze(relative_path: str, text: str) -> CodeAnalysis | None:
    """The analysis for one non-Python source file, or ``None`` when its
    suffix names no language handled here."""

    language = language_for(relative_path)
    spec = LANGUAGES.get(language or "")
    if language is None or spec is None:
        return None
    result = CodeAnalysis(language)
    lines = scan.Lines(text)
    with_strings, bare = scan.mask(text, spec.syntax)

    result.imports = _imports(language, spec, with_strings, bare, lines)
    defined_at: set[int] = set()
    result.symbols = _symbols(spec, bare, lines, defined_at, _Blocks(bare, spec.blocks))
    if spec.calls:
        result.calls = (
            _lisp_calls(bare, lines)
            if spec.blocks == "paren"
            else _calls(spec, bare, lines, defined_at)
        )
    return result


def _imports(
    language: str, spec: _Lang, masked: str, bare: str, lines: scan.Lines
) -> list[CodeImport]:
    found: list[CodeImport] = []
    seen: set[tuple[str, str]] = set()
    go_blocks = (
        [m.span(1) for m in re.finditer(r"(?m)^\s*import\s*\(([^)]*)\)", masked)]
        if language == "go"
        else []
    )
    for pattern, declared in spec.imports:
        for match in pattern.finditer(masked):
            if _inside_string(masked, bare, match.start(), match.end()):
                continue  # `const s = "import x from './y'"` imports nothing
            reference_type = declared
            if declared == "go_import_block":
                # A bare quoted line is an import only inside `import ( ... )`.
                if not any(start <= match.start() < end for start, end in go_blocks):
                    continue
                reference_type = "go_import"
            raw = match.group(1).strip().rstrip(".")
            if not raw or (raw, reference_type) in seen:
                continue
            seen.add((raw, reference_type))
            found.append(CodeImport(raw, reference_type, lines.of(match.start(1))))
    found.sort(key=lambda item: (item.line, item.spec))
    return found


def _inside_string(masked: str, bare: str, start: int, end: int) -> bool:
    """Whether the match begins inside a string literal: its first visible
    character was blanked when string bodies were masked."""

    for k in range(start, end):
        if not masked[k].isspace():
            return bare[k] != masked[k]
    return False


class _Blocks:
    """Bracket pairs for one file, computed once and only if asked for."""

    def __init__(self, masked: str, style: str) -> None:
        self._masked = masked
        self._style = style
        self._pairs: dict[int, int] | None = None

    @property
    def pairs(self) -> dict[int, int]:
        if self._pairs is None:
            opener, closer = ("(", ")") if self._style == "paren" else ("{", "}")
            self._pairs = scan.pairs(self._masked, opener, closer)
        return self._pairs


def _symbols(
    spec: _Lang, masked: str, lines: scan.Lines, defined_at: set[int], blocks: _Blocks
) -> list[CodeSymbol]:
    text_lines = masked.split("\n")
    seen: set[tuple[str, int]] = set()
    symbols: list[CodeSymbol] = []
    for pattern, kind in spec.symbols:
        for match in pattern.finditer(masked):
            name = match.group("name")
            if not name or name in _CONTROL:
                continue
            start = lines.of(match.start("name"))
            if (name, start) in seen:
                continue
            seen.add((name, start))
            defined_at.add(match.start("name"))
            end = _end_line(spec, masked, match, start, lines, text_lines, kind, blocks)
            symbols.append(CodeSymbol(name, kind, start, max(start, end)))
    symbols.sort(key=lambda item: (item.start_line, item.name))
    return _first_clause_only(symbols)


def _first_clause_only(symbols: list[CodeSymbol]) -> list[CodeSymbol]:
    """Erlang and Elixir spell one function as several clauses; keep the first
    of a run of same-named definitions."""

    kept: list[CodeSymbol] = []
    for symbol in symbols:
        if kept and kept[-1].name == symbol.name and kept[-1].kind == symbol.kind:
            last = kept[-1]
            kept[-1] = CodeSymbol(last.name, last.kind, last.start_line, symbol.end_line)
            continue
        kept.append(symbol)
    return kept


def _end_line(
    spec: _Lang,
    masked: str,
    match: re.Match[str],
    start: int,
    lines: scan.Lines,
    text_lines: list[str],
    kind: str,
    blocks: _Blocks,
) -> int:
    if kind == "constant":
        return start
    if spec.blocks == "brace":
        # From the name, not the match end: a pattern may have consumed the
        # body's opening brace, and the search must still see it.
        close = scan.brace_end(masked, match.end("name"), blocks.pairs)
        return lines.of(close) if close is not None else start
    if spec.blocks == "paren":
        open_at = masked.rfind("(", 0, match.start("name"))
        close = scan.paren_end(open_at, blocks.pairs) if open_at >= 0 else None
        return lines.of(close) if close is not None else start
    return scan.indent_end(text_lines, start)


_IDENT = r"[A-Za-z_$][\w$]*"


def _calls(spec: _Lang, masked: str, lines: scan.Lines, defined_at: set[int]) -> list[CodeCall]:
    separators = "|".join(re.escape(sep) for sep in spec.call_separators)
    pattern = re.compile(rf"(?<![\w$.:>]){_IDENT}(?:(?:{separators}){_IDENT})*(?=\s*\()")
    calls: list[CodeCall] = []
    for match in pattern.finditer(masked):
        name = match.group(0)
        last = re.split(separators, name)[-1] if separators else name
        if last in _CONTROL or last in spec.keywords or name in spec.keywords:
            continue
        if match.start() in defined_at or match.end() - len(last) in defined_at:
            continue
        if spec.typed_declarations and _declared_here(masked, match.start()):
            continue
        calls.append(CodeCall(name, lines.of(match.start())))
    return calls


_NOT_A_TYPE = frozenset("return else case throw new await delete yield in of not and or do".split())


def _declared_here(masked: str, start: int) -> bool:
    """``int close(struct conn *c);`` names a function without calling it.

    In a language that declares with a type in front, a name preceded on its
    own line by a word (or ``*``/``&``/``>``) is being declared; one preceded
    by an operator, a bracket, a comma or ``return`` is being called.
    """

    line_start = masked.rfind("\n", 0, start) + 1
    before = masked[line_start:start].rstrip()
    if not before or before.endswith(("=>", "->")):
        return False
    if not (before[-1].isalnum() or before[-1] in "_*&>]"):
        return False
    last_word = re.split(r"[^\w]+", before)[-1]
    return last_word not in _NOT_A_TYPE


def _lisp_calls(masked: str, lines: scan.Lines) -> list[CodeCall]:
    calls: list[CodeCall] = []
    for match in re.finditer(r"\(\s*([A-Za-z_*+!?<>=\-][\w.*+!?<>=/\-]*)", masked):
        name = match.group(1)
        if name in _LISP_SPECIAL or name.startswith(("def", ":")):
            continue
        calls.append(CodeCall(name, lines.of(match.start(1))))
    return calls
