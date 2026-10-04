"""Symbols, imports and calls for every language the graph reads besides Python.

Three layers, each held separately so a failure names the layer:

* **extraction**: one realistic file per language in
  ``tests/fixtures/polyglot/``. Each case pins the imports (spec and reference
  type), the symbols (name, kind) and a set of calls, plus a ``ghost`` defined
  or imported only inside a comment or a string, which must never appear.
* **resolution**: :func:`resolve_code_import` per reference type, including
  the cases that must resolve to nothing (packages, the standard library,
  namespaces with no file).
* **the graph**: the fixture tree synced through the real engine. Every
  importing file gains ``imports`` edges to exactly the files it names, the
  ``symbols`` table carries each language. That a re-sync changes nothing is
  held where every sync guarantee is, in ``tests/test_sync_idempotency.py``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from pheasant.config.schema import PheasantConfig
from pheasant.graph.code_analysis import LANGUAGES, analyze, language_for
from pheasant.graph.code_imports import CODE_REFERENCE_TYPES, resolve_code_import
from pheasant.graph.enrichment import _ArtifactRef, _match_suffix, _node_id
from pheasant.ingestion.content_types import CODE_LANGUAGES
from pheasant.ingestion.notebook import notebook_text
from pheasant.sync.engine import SyncEngine

FIXTURES = Path(__file__).parent / "fixtures" / "polyglot"


def _read(path: str) -> str:
    return (FIXTURES / path).read_text(encoding="utf-8")


# (path, language, imports, symbols, calls that must be found)
CASES = [
    (
        "web/app.ts",
        "typescript",
        {
            ("./store", "js_import"),
            ("../model/item", "js_import"),
            ("react", "js_import"),
            ("./lazy", "js_import"),
        },
        {
            ("App", "class"),
            ("Props", "class"),
            ("Mode", "class"),
            ("render", "function"),
            ("boot", "function"),
            ("handler", "function"),
            ("MAX_ITEMS", "constant"),
        },
        {"formatTitle", "store.load", "logEvent", "Store"},
    ),
    (
        "web/util.js",
        "javascript",
        {("./path-helpers", "js_import")},
        {("helper", "function"), ("parse", "function")},
        {"JSON.parse"},
    ),
    (
        "cmd/server/main.go",
        "go",
        {
            ("fmt", "go_import"),
            ("github.com/acme/app/internal/store", "go_import"),
            ("os", "go_import"),
        },
        {
            ("Server", "class"),
            ("Handler", "class"),
            ("Run", "function"),
            ("main", "function"),
            ("MaxItems", "constant"),
        },
        {"fmt.Println", "st.Open", "NewServer"},
    ),
    (
        "src/lib.rs",
        "rust",
        {
            ("parser", "rust_mod"),
            ("model", "rust_mod"),
            ("crate::model::item::Item", "rust_use"),
            ("std::collections::HashMap", "rust_use"),
            ("serde", "rust_use"),
        },
        {
            ("Engine", "class"),
            ("Runner", "class"),
            ("load", "function"),
            ("helper", "function"),
            ("MAX_ITEMS", "constant"),
        },
        {"parser::parse", "helper"},
    ),
    (
        "src/main/java/com/acme/app/App.java",
        "java",
        {
            ("com.acme.store.Store", "jvm_import"),
            ("com.acme.util.Strings.join", "jvm_import"),
            ("java.util.*", "jvm_import"),
        },
        {
            ("App", "class"),
            ("render", "function"),
            ("close", "function"),
            ("MAX_ITEMS", "constant"),
        },
        {"Store.open", "Store.shutdown", "join"},
    ),
    (
        "app/src/Main.kt",
        "kotlin",
        {("com.acme.store.Store", "jvm_import"), ("kotlinx.coroutines.launch", "jvm_import")},
        {("Item", "class"), ("Registry", "class"), ("register", "function"), ("main", "function")},
        {"Store.save", "Registry.register"},
    ),
    (
        "src/Main.scala",
        "scala",
        {("acme.store.Store", "jvm_import"), ("scala.collection.mutable", "jvm_import")},
        {("Runner", "class"), ("Main", "class"), ("run", "function")},
        {"Store.open"},
    ),
    (
        "build.gradle",
        "groovy",
        set(),
        {("configure", "function")},
        {"project.apply"},
    ),
    (
        "src/net/socket.c",
        "c",
        {
            ("socket.h", "c_include"),
            ("../util/log.h", "c_include"),
            ("stdio.h", "c_system_include"),
        },
        {("conn", "class"), ("open_socket", "function"), ("MAX_CONNS", "constant")},
        {"log_info", "connect_to"},
    ),
    (
        "src/engine.cpp",
        "cpp",
        {("engine.hpp", "c_include"), ("vector", "c_system_include")},
        {("Engine", "class"), ("run", "function"), ("count", "function")},
        {"loadItems", "std::sort", "helpers::countAll"},
    ),
    (
        "src/App.cs",
        "csharp",
        {
            ("System", "csharp_using"),
            ("Acme.Store", "csharp_using"),
            ("System.Math", "csharp_using"),
        },
        {
            ("App", "class"),
            ("IRenderer", "class"),
            ("RenderAsync", "function"),
            ("Dispose", "function"),
            ("Name", "constant"),
        },
        {"Store.LoadAsync", "Format", "Store.Release"},
    ),
    (
        "lib/acme/app.rb",
        "ruby",
        {("json", "ruby_require"), ("store", "ruby_require_relative")},
        {
            ("Acme", "class"),
            ("App", "class"),
            ("render", "function"),
            ("build", "function"),
            ("MAX_ITEMS", "constant"),
        },
        {"Store.find", "format_item"},
    ),
    (
        "src/App.php",
        "php",
        {("App\\Models\\User", "php_use"), ("config/app.php", "php_include")},
        {("App", "class"), ("render", "function"), ("helper", "function"), ("VERSION", "constant")},
        {"User::find", "$this->format", "strtoupper"},
    ),
    (
        "Sources/App/App.swift",
        "swift",
        {("Foundation", "swift_import"), ("AppCore", "swift_import")},
        {("Item", "class"), ("Renderer", "class"), ("App", "class"), ("render", "function")},
        {"Store.load", "Store.clear", "format"},
    ),
    (
        "lib/src/app.dart",
        "dart",
        {
            ("package:flutter/material.dart", "dart_import"),
            ("package:acme/store.dart", "dart_import"),
            ("widgets/button.dart", "dart_import"),
            ("dart:async", "dart_import"),
        },
        {("App", "class"), ("build", "function"), ("main", "function")},
        {"runApp", "Button", "Store.save"},
    ),
    (
        "src/main.zig",
        "zig",
        {("std", "zig_import"), ("store.zig", "zig_import")},
        {("Item", "class"), ("main", "function"), ("helper", "function")},
        {"store.load", "std.debug.print"},
    ),
    (
        "src/App/Main.hs",
        "haskell",
        {("Data.Map", "haskell_import"), ("App.Store", "haskell_import")},
        {("Item", "class"), ("render", "function"), ("main", "function")},
        set(),  # application without parentheses: calls are not extracted
    ),
    (
        "lib/acme/app.ex",
        "elixir",
        {("Acme.Store", "elixir_module"), ("Ecto.Query", "elixir_module")},
        {("Acme.App", "class"), ("render", "function"), ("format_item", "function")},
        {"Store.fetch", "format_item", "inspect"},
    ),
    (
        "src/acme_app.erl",
        "erlang",
        {("acme.hrl", "erlang_include"), ("acme_store", "erlang_module")},
        {("item", "class"), ("render", "function"), ("format", "function")},
        {"acme_store:load", "io_lib:format", "format"},
    ),
    (
        "src/acme/app.clj",
        "clojure",
        {("acme.store-api.core", "clojure_ns"), ("clojure.string", "clojure_ns")},
        {("Item", "class"), ("render", "function"), ("helper", "function")},
        {"store/fetch", "str/upper-case", "render"},
    ),
    (
        "lua/app.lua",
        "lua",
        {("acme.store", "lua_require")},
        {("helper", "function"), ("App.render", "function")},
        {"store.load", "helper"},
    ),
    (
        "scripts/deploy.sh",
        "shell",
        {("./lib/common.sh", "shell_source"), ("$HOME/.profile", "shell_source")},
        {("deploy", "function"), ("rollback", "function")},
        set(),
    ),
    (
        "proto/store.proto",
        "protobuf",
        {("common/types.proto", "proto_import")},
        {("Item", "class"), ("Store", "class"), ("Load", "function")},
        set(),
    ),
]


@pytest.mark.parametrize(
    ("path", "language", "imports", "symbols", "calls"), CASES, ids=[c[1] for c in CASES]
)
def test_each_language_yields_its_imports_symbols_and_calls(
    path: str, language: str, imports: set, symbols: set, calls: set
) -> None:
    analysis = analyze(path, _read(path))
    assert analysis is not None
    assert analysis.language == language
    assert {(item.spec, item.reference_type) for item in analysis.imports} == imports
    assert symbols <= {(symbol.name, symbol.kind) for symbol in analysis.symbols}
    found_calls = {call.name for call in analysis.calls}
    assert calls <= found_calls
    # Every sample hides a `ghost` in a comment or a string; none may surface.
    names = (
        {item.spec for item in analysis.imports}
        | {symbol.name for symbol in analysis.symbols}
        | found_calls
    )
    assert not any("ghost" in name.lower() or "fake" in name.lower() for name in names)


def test_every_case_covers_a_distinct_language_and_all_languages_are_covered() -> None:
    covered = {case[1] for case in CASES}
    analysed = set(CODE_LANGUAGES.values()) - {"python", "objc"}
    assert covered == analysed
    assert set(LANGUAGES) == analysed | {"objc"}


def test_symbol_spans_follow_the_block_not_the_signature() -> None:
    spans = {
        (symbol.name, symbol.start_line, symbol.end_line)
        for symbol in analyze("src/net/socket.c", _read("src/net/socket.c")).symbols
    }
    # A body with an inner block, and an opening brace on its own line.
    assert ("open_socket", 12, 19) in spans
    go = {
        s.name: (s.start_line, s.end_line)
        for s in analyze("m.go", _read("cmd/server/main.go")).symbols
    }
    assert go["Run"] == (21, 24)
    ruby = {
        s.name: (s.start_line, s.end_line)
        for s in analyze("a.rb", _read("lib/acme/app.rb")).symbols
    }
    assert ruby["render"] == (9, 12)  # the closing `end` belongs to it


def test_an_expression_bodied_function_does_not_borrow_the_next_body() -> None:
    text = "fun one() = 1\n\nfun two(): Int {\n    return 2\n}\n"
    spans = {s.name: (s.start_line, s.end_line) for s in analyze("a.kt", text).symbols}
    assert spans["one"] == (1, 1)
    assert spans["two"] == (3, 5)


def test_a_prototype_is_a_declaration_not_a_call() -> None:
    calls = {c.name for c in analyze("a.c", "int close_socket(int fd);\nvoid f() { g(); }\n").calls}
    assert calls == {"g"}


def test_python_and_unknown_suffixes_are_not_handled_here() -> None:
    assert analyze("a.py", "import os\n") is None
    assert analyze("notes.md", "# x\n") is None
    assert language_for("src/Main.KT") == "kotlin"


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

INDEXED = [
    "web/store.ts",
    "web/lazy/index.js",
    "web/util.ts",
    "internal/store/store.go",
    "internal/store/db.go",
    "internal/store/store_test.go",
    "errors/errors.go",
    "src/parser.rs",
    "src/model/mod.rs",
    "src/model/item.rs",
    "src/main/java/com/acme/store/Store.java",
    "app/com/acme/store/Store.kt",
    "src/net/socket.h",
    "include/acme/log.h",
    "lib/acme/store.rb",
    "src/Models/User.php",
    "lib/store.dart",
    "App/Store.hs",
    "lib/my_app/store.ex",
    "src/acme_store.erl",
    "src/acme/store_api/core.clj",
    "lua/acme/store.lua",
    "lua/acme/net/init.lua",
]


def _resolve(spec: str, reference_type: str, importer: str) -> list[str]:
    by_path = {p.lower(): [_ArtifactRef(p, "s", p)] for p in INDEXED}
    hits = resolve_code_import(
        spec, reference_type, importer, by_path, lambda c: _match_suffix(c, by_path)
    )
    return [hit.relative_path for hit in hits]


@pytest.mark.parametrize(
    ("spec", "reference_type", "importer", "expected"),
    [
        ("./store", "js_import", "web/app.ts", ["web/store.ts"]),
        ("./lazy", "js_import", "web/app.ts", ["web/lazy/index.js"]),
        ("./util.js", "js_import", "web/app.ts", ["web/util.ts"]),  # TS ESM convention
        ("../web/store", "js_import", "pages/home.tsx", ["web/store.ts"]),
        ("react", "js_import", "web/app.ts", []),
        (
            "github.com/acme/app/internal/store",
            "go_import",
            "cmd/main.go",
            [
                "internal/store/db.go",
                "internal/store/store.go",
            ],
        ),  # fmt: skip
        ("errors", "go_import", "cmd/main.go", []),  # one segment is the stdlib
        ("parser", "rust_mod", "src/lib.rs", ["src/parser.rs"]),
        ("item", "rust_mod", "src/model/mod.rs", ["src/model/item.rs"]),
        ("crate::model::item::Item", "rust_use", "src/lib.rs", ["src/model/item.rs"]),
        ("std::fmt", "rust_use", "src/lib.rs", []),
        (
            "com.acme.store.Store",
            "jvm_import",
            "x/App.java",
            ["src/main/java/com/acme/store/Store.java"],
        ),  # fmt: skip
        ("com.acme.store.Store", "jvm_import", "x/Main.kt", ["app/com/acme/store/Store.kt"]),
        ("java.util.*", "jvm_import", "x/App.java", []),
        ("socket.h", "c_include", "src/net/socket.c", ["src/net/socket.h"]),
        ("acme/log.h", "c_include", "src/net/socket.c", ["include/acme/log.h"]),
        ("stdio.h", "c_system_include", "src/net/socket.c", []),
        ("Acme.Store", "csharp_using", "src/App.cs", []),
        ("store", "ruby_require_relative", "lib/acme/app.rb", ["lib/acme/store.rb"]),
        ("acme/store", "ruby_require", "bin/run.rb", ["lib/acme/store.rb"]),
        ("App\\Models\\User", "php_use", "src/App.php", ["src/Models/User.php"]),
        ("package:acme/store.dart", "dart_import", "lib/src/app.dart", ["lib/store.dart"]),
        ("dart:async", "dart_import", "lib/src/app.dart", []),
        ("Foundation", "swift_import", "App.swift", []),
        ("App.Store", "haskell_import", "App/Main.hs", ["App/Store.hs"]),
        ("MyApp.Store", "elixir_module", "lib/my_app.ex", ["lib/my_app/store.ex"]),
        ("acme_store", "erlang_module", "src/acme_app.erl", ["src/acme_store.erl"]),
        ("acme.store-api.core", "clojure_ns", "src/acme/app.clj", ["src/acme/store_api/core.clj"]),
        ("acme.store", "lua_require", "lua/app.lua", ["lua/acme/store.lua"]),
        ("acme.net", "lua_require", "lua/app.lua", ["lua/acme/net/init.lua"]),
    ],
)
def test_imports_resolve_to_the_files_they_name(
    spec: str, reference_type: str, importer: str, expected: list[str]
) -> None:
    assert _resolve(spec, reference_type, importer) == expected


def test_a_relative_import_is_resolved_against_its_own_importer() -> None:
    # One external_reference node serves every `./store`; the edge, not the
    # node, says which file asked.
    assert _resolve("./store", "js_import", "web/app.ts") == ["web/store.ts"]
    assert _resolve("./store", "js_import", "admin/app.ts") == []
    assert _resolve("./store", "js_import", None) == []  # type: ignore[arg-type]


def test_every_emitted_reference_type_is_known_to_the_resolver() -> None:
    emitted = {
        item.reference_type for path, *_ in CASES for item in analyze(path, _read(path)).imports
    }
    assert emitted <= CODE_REFERENCE_TYPES


# --------------------------------------------------------------------------
# The graph
# --------------------------------------------------------------------------


def _engine(tmp_path: Path) -> SyncEngine:
    workspace = tmp_path / "ws"
    shutil.copytree(FIXTURES, workspace)
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": "polyglot",
                "state_path": str(tmp_path / "state"),
                "workspace_root": str(workspace),
                "exports_path": str(tmp_path / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "sources": [{"name": "poly", "type": "document_folder", "path": str(workspace)}],
        }
    )
    return SyncEngine(config)


def _resolved_imports(engine: SyncEngine) -> dict[str, set[str]]:
    graph = engine.graph_builder.graph
    paths = {
        node_id: attrs["relative_path"]
        for node_id, attrs in graph.iter_nodes()
        if attrs.get("type") in {"file", "document", "markdown_note"} and attrs.get("relative_path")
    }
    out: dict[str, set[str]] = {}
    for edge in graph.to_node_link()["links"]:
        if edge.get("type") == "imports" and edge["source"] in paths and edge["target"] in paths:
            out.setdefault(paths[edge["source"]], set()).add(paths[edge["target"]])
    return out


EXPECTED_GRAPH = {
    "web/app.ts": {"web/store.ts", "model/item.ts", "web/lazy/index.js"},
    "web/util.js": {"web/path-helpers.js"},
    "cmd/server/main.go": {"internal/store/store.go"},
    "src/lib.rs": {"src/parser.rs", "src/model/mod.rs", "src/model/item.rs"},
    "src/model/mod.rs": {"src/model/item.rs"},
    "src/main/java/com/acme/app/App.java": {
        "src/main/java/com/acme/store/Store.java",
        "src/main/java/com/acme/util/Strings.java",
    },
    "app/src/Main.kt": {"app/src/com/acme/store/Store.kt"},
    "src/Main.scala": {"src/acme/store/Store.scala"},
    "src/net/socket.c": {"src/net/socket.h", "src/util/log.h"},
    "src/engine.cpp": {"src/engine.hpp"},
    "lib/acme/app.rb": {"lib/acme/store.rb"},
    "src/App.php": {"src/config/app.php", "src/Models/User.php"},
    "lib/src/app.dart": {"lib/store.dart", "lib/src/widgets/button.dart"},
    "src/main.zig": {"src/store.zig"},
    "src/App/Main.hs": {"src/App/Store.hs"},
    "lib/acme/app.ex": {"lib/acme/store.ex"},
    "src/acme_app.erl": {"src/acme_store.erl", "src/acme.hrl"},  # .hrl is Erlang too
    "src/acme/app.clj": {"src/acme/store_api/core.clj"},
    "lua/app.lua": {"lua/acme/store.lua"},
    "scripts/deploy.sh": {"scripts/lib/common.sh"},
    "proto/store.proto": {"proto/common/types.proto"},
}


def test_a_polyglot_repository_gains_file_to_file_import_edges(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    try:
        engine.sync_source("poly", "full")
        assert _resolved_imports(engine) == EXPECTED_GRAPH
        languages = {
            row["language"] for row in engine.state.rows("SELECT DISTINCT language FROM symbols")
        }
        assert set(CODE_LANGUAGES.values()) - {"python", "objc"} <= languages
        calls = {
            (attrs.get("language"), attrs.get("name"))
            for _, attrs in engine.graph_builder.graph.iter_nodes()
            if attrs.get("symbol_type") == "call_target"
        }
        assert ("go", "st.Open") in calls
        assert ("rust", "parser::parse") in calls
        assert ("typescript", "formatTitle") in calls
    finally:
        engine.close()


def test_the_wasm_path_hands_code_imports_to_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """The compiled guest knows Python imports and links only. Acceleration
    must not drop the edges the default path draws for every other language."""

    from pheasant.graph.enrichment import resolve_cross_source_edges
    from pheasant.sandbox.accel import cross_source

    seen: list[list] = []

    def fake_guest(nodes, edges):
        seen.append(edges)
        return resolve_cross_source_edges(nodes, edges)

    monkeypatch.setattr(cross_source, "_resolve_in_guest", fake_guest)
    nodes = [
        ("file:a", {"type": "file", "relative_path": "web/app.ts", "source_id": "s"}),
        ("file:b", {"type": "file", "relative_path": "web/store.ts", "source_id": "s"}),
        ("file:c", {"type": "file", "relative_path": "pkg/mod.py", "source_id": "s"}),
        ("file:d", {"type": "file", "relative_path": "main.py", "source_id": "s"}),
        ("ext:1", {"type": "external_reference", "reference": "./store", "source_id": "s"}),
        ("ext:2", {"type": "external_reference", "reference": "pkg.mod", "source_id": "s"}),
    ]
    edges = [
        ("file:a", "ext:1", "imports", "js_import"),
        ("file:d", "ext:2", "imports", "python_import"),
    ]
    accelerated = cross_source.resolve_cross_source_edges_wasm(nodes, edges)
    assert seen == [[("file:d", "ext:2", "imports", "python_import")]]
    assert accelerated == resolve_cross_source_edges(nodes, edges)
    assert {(e.source, e.target) for e in accelerated} == {
        ("file:a", "file:b"),
        ("file:d", "file:c"),
    }


def test_python_analysis_and_its_ids_are_unchanged(tmp_path: Path) -> None:
    """Persisted graphs hold these ids (rule 3). Python still goes through
    ``ast``, labels its symbols ``python`` and keeps the call-target id it
    always had; only the other languages gain a language segment."""

    from pheasant.config.schema import SourceConfig, SourceType
    from pheasant.graph.enrichment import CodeEnrichmentPass
    from pheasant.ingestion.pipeline import parse_file

    (tmp_path / "mod.py").write_text("import os\n\ndef run():\n    os.walk('.')\n")
    source = SourceConfig(name="src", type=SourceType.document_folder, path=tmp_path)
    artifact = parse_file(source, tmp_path / "mod.py")
    enrichment = CodeEnrichmentPass().run("kb", source, artifact)
    assert {s["language"] for s in enrichment.symbols} == {"python"}
    call_ids = {n.id for n in enrichment.nodes if n.attrs.get("symbol_type") == "call_target"}
    assert call_ids == {_node_id("symbol", "kb", "src", "call", "os.walk")}


# --------------------------------------------------------------------------
# Notebooks
# --------------------------------------------------------------------------


def test_a_notebook_is_read_as_its_cells_without_outputs() -> None:
    notebook = {
        "metadata": {"language_info": {"name": "python"}},
        "cells": [
            {"cell_type": "markdown", "source": ["# Analysis\n", "Load the data."]},
            {
                "cell_type": "code",
                "source": "df = load()\n",
                "outputs": [{"data": {"image/png": "iVBORw0KGgo" * 50}}],
            },
            {"cell_type": "code", "source": ""},
        ],
    }
    text = notebook_text(json.dumps(notebook))
    assert text == "# Analysis\nLoad the data.\n\n```python\ndf = load()\n```\n"
    assert "iVBOR" not in text
    assert notebook_text("not json") == "not json"


# --------------------------------------------------------------------------
# Cost: linear in the file, whatever the file holds
# --------------------------------------------------------------------------

#: Shapes that once made a pattern or a block search quadratic: a construct
#: that opens and never closes, repeated. Generated files and minified bundles
#: look like this, and this runs on the indexing path.
HOSTILE = {
    "a.c": "int alpha beta gamma delta epsilon\n",
    "b.cpp": "class A : public B, public C, public D\n",
    "c.java": "  public static final java.util.Map<String, Integer> name(int a, int b\n",
    "d.js": "const a = async (b, c, d) => e\n",
    "e.dart": "Future<List<int>> load(int a, int b\n",
    "f.ts": "import a b c d e f g\n",
    "g.clj": "(defn a [x] (b ",
    "h.go": "func a() {\n",
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_analysis_cost_grows_linearly_on_hostile_input(name: str) -> None:
    """A ratio, not a stopwatch: 4x the input must cost well under the 16x a
    quadratic pass would, so the runner's speed cancels out. Minimum of three
    runs, because the question is the algorithm and not the scheduler."""

    import time

    def cost(repeats: int) -> float:
        text = HOSTILE[name] * repeats
        best = float("inf")
        for _ in range(3):
            started = time.perf_counter()
            analyze(name, text)
            best = min(best, time.perf_counter() - started)
        return best

    small, large = cost(1_000), cost(4_000)
    assert large < 10 * max(small, 1e-4), (name, small, large)
