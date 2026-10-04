"""Resolve a non-Python import to the indexed files it names.

:mod:`pheasant.graph.code_analysis` records each import as the specifier the
source wrote, typed by language family. Some of those name a file
(``import "./util"``, ``#include "net/socket.h"``, ``mod parser;``); some name a
package this region does not hold (``react``, ``<stdio.h>``, ``std::fmt``); and
some name a namespace that has no file at all (C#'s ``using``, Swift's
``import``). This module answers only the first kind and returns nothing for
the other two, which is the honest answer: an edge to a guessed file is worse
than no edge, because the graph walk cannot tell the difference.

Two strategies, by what the language promises:

* **relative** specifiers are joined to the importing file's directory and
  must match exactly (with the language's implied suffixes and index files);
* **qualified** names (``com.acme.Store``, ``crate::model::item``,
  ``App\\Models\\User``) are mapped to a path and matched as a *suffix* of an
  indexed path, the rule Python imports already use, because the root they
  are relative to (``src/``, ``lib/``, a classpath) is not in the source.

Deterministic and allocation-light: the per-language candidates are a handful
of strings, and the suffix match is the caller's.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

T = TypeVar("T")
SuffixMatch = Callable[[str], list[Any]]

_JS_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte")
_JVM_SUFFIXES = (".java", ".kt", ".kts", ".scala", ".groovy")

#: Reference types this module resolves. Anything else (``js_package`` style
#: bare names land in ``js_import`` and are filtered below) yields no edge.
CODE_REFERENCE_TYPES = frozenset(
    {
        "js_import",
        "go_import",
        "rust_mod",
        "rust_use",
        "jvm_import",
        "c_include",
        "c_system_include",
        "csharp_using",
        "ruby_require",
        "ruby_require_relative",
        "php_include",
        "php_use",
        "swift_import",
        "dart_import",
        "zig_import",
        "haskell_import",
        "elixir_module",
        "erlang_include",
        "erlang_module",
        "clojure_ns",
        "lua_require",
        "shell_source",
        "proto_import",
    }
)


def resolve_code_import(
    spec: str,
    reference_type: str,
    importer: str | None,
    by_path: dict[str, list[T]],
    match_suffix: Callable[[str], list[T]],
) -> list[T]:
    """Indexed artifacts ``spec`` names, from the file at ``importer``.

    ``by_path`` is keyed by lower-cased, slash-normalised relative path, the
    same index :func:`pheasant.graph.enrichment.resolve_cross_source_edges`
    builds. ``importer`` is the importing artifact's relative path; without it
    a relative specifier cannot be resolved and is not guessed at.
    """

    base = posixpath.dirname(_norm(importer)) if importer else None

    def exact(candidates: Sequence[str]) -> list[T]:
        for candidate in candidates:
            hit = by_path.get(_norm(candidate))
            if hit:
                return hit
        return []

    def suffix(candidates: Sequence[str]) -> list[T]:
        for candidate in candidates:
            hit = match_suffix(candidate)
            if hit:
                return hit
        return []

    def joined(path: str) -> str | None:
        if base is None:
            return None
        return posixpath.normpath(posixpath.join(base, path)) if base else posixpath.normpath(path)

    if reference_type == "js_import":
        if not spec.startswith("."):
            return []  # a package, or a bundler alias this region cannot see
        target = joined(spec)
        return exact(_js_candidates(target)) if target else []
    if reference_type == "go_import":
        return _go_package(spec, by_path)
    if reference_type == "rust_mod":
        return exact(_rust_mod_candidates(spec, importer)) if importer else []
    if reference_type == "rust_use":
        return suffix(_rust_use_candidates(spec))
    if reference_type == "jvm_import":
        parts = spec.rstrip("*").rstrip(".").split(".")
        if spec.endswith("*") or len(parts) < 2:
            return []
        # The importer's own language first: a Kotlin file importing
        # `acme.Store` means Store.kt when both Store.kt and Store.java exist.
        own = posixpath.splitext(_norm(importer or ""))[1]
        order = sorted(_JVM_SUFFIXES, key=lambda ext: ext != own)
        stem = "/".join(parts)
        outer = "/".join(parts[:-1])  # `import static a.B.member`, `import a.B.Inner`
        return suffix([stem + ext for ext in order] + [outer + ext for ext in order])
    if reference_type in {"c_include", "erlang_include", "zig_import", "shell_source"}:
        if reference_type == "zig_import" and not spec.endswith(".zig"):
            return []  # `@import("std")`
        target = joined(spec)
        return (exact([target]) if target else []) or suffix([spec])
    if reference_type == "proto_import":
        return suffix([spec])
    if reference_type == "ruby_require_relative":
        target = joined(spec)
        return exact([target, target + ".rb"]) if target else []
    if reference_type == "ruby_require":
        return suffix([spec + ".rb", spec])
    if reference_type == "php_include":
        target = joined(spec)
        return (exact([target]) if target else []) or suffix([spec.lstrip("./")])
    if reference_type == "php_use":
        # PSR-4 maps a namespace prefix to a directory (`App\\` -> `src/`), so
        # the leading segments may be absent from the path; keep two.
        segments = [part for part in spec.split("\\") if part]
        return suffix(["/".join(segments[k:]) + ".php" for k in range(max(1, len(segments) - 1))])
    if reference_type == "dart_import":
        if spec.startswith("dart:"):
            return []
        if spec.startswith("package:"):
            package_path = spec.removeprefix("package:").split("/", 1)
            return suffix(["lib/" + package_path[1]]) if len(package_path) == 2 else []
        target = joined(spec)
        return exact([target]) if target else []
    if reference_type == "haskell_import":
        stem = spec.replace(".", "/")
        return suffix([stem + ".hs", stem + ".lhs"])
    if reference_type == "elixir_module":
        stem = "/".join(_snake(part) for part in spec.split("."))
        return suffix([stem + ".ex", stem + ".exs"])
    if reference_type == "erlang_module":
        return suffix([spec + ".erl"])
    if reference_type == "clojure_ns":
        stem = spec.replace("-", "_").replace(".", "/")
        return suffix([stem + ext for ext in (".clj", ".cljs", ".cljc")])
    if reference_type == "lua_require":
        stem = spec.replace(".", "/")
        return suffix([stem + ".lua", stem + "/init.lua"])
    return []  # c_system_include, csharp_using, swift_import: no file to name


def _js_candidates(target: str) -> list[str]:
    stem, ext = posixpath.splitext(target)
    candidates = [target]
    if ext in {".js", ".jsx", ".mjs", ".cjs"}:
        # TypeScript's ESM convention: `./util.js` in source names util.ts.
        candidates += [stem + ".ts", stem + ".tsx", stem + ".mts", stem + ".cts"]
    candidates += [target + suffix for suffix in _JS_SUFFIXES]
    candidates += [f"{target}/index{suffix}" for suffix in _JS_SUFFIXES]
    return candidates


def _rust_mod_candidates(name: str, importer: str) -> list[str]:
    directory = posixpath.dirname(_norm(importer))
    stem = posixpath.splitext(posixpath.basename(_norm(importer)))[0]
    # `mod x;` in lib.rs / main.rs / mod.rs declares a sibling; anywhere else
    # it declares a child of the file's own module directory (Rust 2018).
    if stem not in {"lib", "main", "mod"}:
        directory = posixpath.join(directory, stem) if directory else stem
    base = posixpath.join(directory, name) if directory else name
    return [base + ".rs", base + "/mod.rs"]


def _rust_use_candidates(spec: str) -> list[str]:
    parts = [part for part in spec.split("::") if part]
    if not parts or parts[0] != "crate":
        return []  # std, external crates; `self`/`super` need the module tree
    parts = parts[1:]
    candidates: list[str] = []
    # The last segment may be an item inside the module, not a module.
    while parts:
        stem = "/".join(parts)
        candidates += [stem + ".rs", stem + "/mod.rs"]
        parts = parts[:-1]
    return candidates


def _go_package(spec: str, by_path: dict[str, list[T]]) -> list[T]:
    """Every non-test ``.go`` file in the directory the import path ends with.

    A Go import names a package, which is a directory. The module prefix
    (``github.com/acme/app``) is in ``go.mod``, not in the path, so the longest
    trailing run of at least two segments that matches an indexed directory
    wins. One segment is never enough: ``fmt`` and ``errors`` are the standard
    library, and a local ``errors/`` package must not capture them.
    """

    segments = [part for part in spec.strip("/").split("/") if part]
    by_dir: dict[str, list[str]] = {}
    for path in by_path:
        if path.endswith(".go") and not path.endswith("_test.go"):
            by_dir.setdefault(posixpath.dirname(path), []).append(path)
    for size in range(len(segments), 1, -1):
        wanted = "/".join(segments[-size:]).lower()
        matches = sorted(
            directory
            for directory in by_dir
            if directory == wanted or directory.endswith("/" + wanted)
        )
        if matches:
            hits: list[T] = []
            for directory in matches:
                for path in sorted(by_dir[directory]):
                    hits.extend(by_path[path])
            return hits
    return []


def _snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _norm(value: str) -> str:
    return value.replace("\\", "/").strip("/").lower()
