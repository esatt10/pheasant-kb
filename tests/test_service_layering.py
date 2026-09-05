"""transport → services → domain → persistence, with no upward edges.

The application layer is only worth having if it cannot be bypassed. Before it
existed the dependencies ran in every direction: `mcp_server/tools.py` imported
graph helpers and a help-text constant out of `api/app.py`, and so did
`assistant/retrieval.py` — neither for anything to do with HTTP, only because
that is where the code happened to live. A tool layer that imports a web
transport is a tool layer that inherits its release cycle, its dependencies and
its bugs.

These tests are the contract. They are deliberately import-graph tests rather
than a linter's configuration file: the rule is short enough to state, and
stating it in the test suite means it fails in the same run as everything else
rather than in a separate tool somebody has to remember to install.

The one permitted upward import is a **composition root**: `cli.py` and the
benchmark build the app in order to serve it. That is what a composition root
is for, and it is the reason this checks *which* module imports upward rather
than banning it outright.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

SOURCE_ROOT = REPO_ROOT / "src" / "pheasant"

#: Layer per module path, resolved by longest declared prefix. Lower numbers
#: may not import higher ones.
#:
#: Three layers, not more. The boundary this enforces is the one the service
#: extraction created — transport above services above everything else — and a
#: finer ordering *within* the domain is not something anybody designed. Making
#: one up here would produce a test that fails for reasons unrelated to the
#: rule it is named after, which is worse than not having it: `persistence`
#: reaches for a timestamp helper in `ingestion`, and that is a tidiness
#: question, not a layering violation.
#:
#: Prefixes rather than top-level packages only because one package genuinely
#: spans two levels, and the test is what established that — see `assistant`.
LAYERS: dict[str, int] = {
    # Transport: parse a request, call one operation, marshal the answer.
    "api": 3,
    "mcp_server": 3,
    "cli": 3,
    # Application: one implementation per operation, transport-neutral. The
    # assistant orchestrates retrieval to answer a question, which is an
    # application concern rather than a domain primitive.
    "services": 2,
    "assistant": 2,
    # The readiness plane orchestrates the service layer to establish whether
    # this region can be measured by somebody else. Application, like
    # `services` — it calls operations, it does not implement one.
    "readiness": 2,
    # ...except for its model-access adapters, which are infrastructure and sit
    # below everything that orchestrates. This is not a carve-out to make the
    # test pass: `memory/synthesis.py` wants a model handle and `setup_wizard`
    # wants the provider table, and neither has any business reaching the
    # answering pipeline. Declaring the split here says so, and keeps the edge
    # that would actually be wrong — `memory` importing `assistant.chat` — a
    # failure.
    "assistant/catalog": 1,
    "assistant/providers": 1,
    "assistant/llm": 1,
    "assistant/credentials": 1,
    # Domain and below. Listed explicitly rather than defaulted so a new
    # package has to say where it belongs.
    "search": 1,
    "graph": 1,
    "memory": 1,
    "sync": 1,
    "ingestion": 1,
    "evaluation": 1,
    "tuning": 1,
    "telemetry": 1,
    "security": 1,
    "synapse": 1,
    "connectors": 1,
    "sandbox": 1,
    "registry": 1,
    "deployment": 1,
    "config": 1,
    "persistence": 1,
    "decision": 1,
    "analytics": 1,
    "capacity": 1,
    "evalset": 1,
    "jobs": 1,
    "sharding": 1,
    # Config *generators*, not request handlers: they emit a YAML file or a
    # client's JSON and serve nothing. Classifying them as transport would put
    # `deployment`, which composes their output, below them for no reason.
    "mcp_client": 1,
    "quickstart": 1,
    "setup_wizard": 1,
}

#: Modules allowed to import a transport, because building or driving one is
#: their job. A composition root is not an exception to the rule; it is what
#: the rule is for.
COMPOSITION_ROOTS = {
    "cli.py",
    "__main__.py",
    "evaluation/benchmark.py",
    # Measures the MCP facade's latency, so it has to construct one.
    "memory/benchmark.py",
    # Drives both transports to establish that they expose what the capability
    # contract claims. Same justification as `memory/benchmark.py` and the one
    # this set's own docstring gives: a module whose subject *is* a transport
    # has to be able to look at it. The alternative was to assert the
    # contract-to-facade agreement only in this suite, which proves it in CI
    # and leaves a deployed region unable to answer the question a harness's
    # preflight actually asks. It is asserted in both places.
    "readiness/probes/identity.py",
}


def _layer_of(relative: str) -> int | None:
    """The layer of the longest declared prefix of a `pkg/module.py` path."""

    parts = relative.removesuffix(".py").split("/")
    for depth in range(len(parts), 0, -1):
        declared = LAYERS.get("/".join(parts[:depth]))
        if declared is not None:
            return declared
    return None


def _imports(path: Path) -> set[str]:
    """`pheasant.x.y` module paths this file imports, at any nesting depth."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("pheasant"):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("pheasant"):
                    found.add(alias.name)
    return found


def _module_files() -> list[Path]:
    return [p for p in sorted(SOURCE_ROOT.rglob("*.py")) if p.name != "__init__.py"]


def test_no_module_imports_a_layer_above_its_own() -> None:
    """The rule, over the whole tree."""

    offenders: list[str] = []
    for path in _module_files():
        relative = str(path.relative_to(SOURCE_ROOT))
        if relative in COMPOSITION_ROOTS:
            continue
        own = _layer_of(relative)
        if own is None:
            continue
        for imported in _imports(path):
            target = imported.removeprefix("pheasant.")
            other = _layer_of(target.replace(".", "/"))
            if other is not None and other > own:
                offenders.append(f"{relative} imports {imported}")
    assert not offenders, (
        "upward imports break the layering (transport → services → domain → persistence):\n  "
        + "\n  ".join(sorted(offenders))
        + "\nMove the shared thing down into the layer that both callers can reach."
    )


def test_the_service_layer_imports_no_transport() -> None:
    """The specific edge the extraction removed, asserted directly.

    A service that imported `api` or `mcp_server` would be a service in name
    only — it could not be called by the other surface without dragging a web
    framework or an SDK in with it.
    """

    offenders: list[str] = []
    for path in sorted((SOURCE_ROOT / "services").rglob("*.py")):
        for imported in _imports(path):
            if imported.startswith(("pheasant.api", "pheasant.mcp_server", "pheasant.cli")):
                offenders.append(f"{path.name} imports {imported}")
    assert not offenders, offenders


@pytest.mark.parametrize("module", ["assistant/retrieval.py", "mcp_server/tools.py"])
def test_the_modules_that_reached_into_http_no_longer_do(module: str) -> None:
    """Both imported graph helpers out of `api/app.py` — neither for anything
    to do with HTTP. That is the import the service layer exists to delete."""

    imports = _imports(SOURCE_ROOT / module)
    assert not any(name.startswith("pheasant.api") for name in imports), (
        f"{module} still imports from the HTTP transport: "
        f"{sorted(n for n in imports if n.startswith('pheasant.api'))}"
    )


def test_every_package_declares_its_layer() -> None:
    """A package with no layer is one nobody decided about, and the rule above
    silently skips it."""

    packages = {
        path.relative_to(SOURCE_ROOT).parts[0].removesuffix(".py") for path in _module_files()
    }
    undeclared = sorted(packages - set(LAYERS))
    # Leaf modules at the root with no dependents worth ordering.
    undeclared = [
        name for name in undeclared if name not in {"version", "targets", "testing", "__main__"}
    ]
    assert not undeclared, (
        f"these packages have no declared layer: {undeclared}. Add them to LAYERS — a package "
        "whose layer nobody stated is one the rule cannot check."
    )
