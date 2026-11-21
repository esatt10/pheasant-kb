"""PR gate: the config surface must stay reachable and documented.

This replaces ``tests/test_config_wizard_freshness.py``, which guarded a
449-line prose wizard against the schema drifting out from under it. That
wizard is gone, and with it most of the rot it was built to detect: the
interactive wizard reads its defaults off the live dataclasses
(:func:`pheasant.setup_wizard.schema_default`), so a changed default cannot
go stale there.

Two mechanical checks survive, because they are the ones live code cannot
enforce for itself:

1. **Reachability** — every top-level config section is claimed by a
   :class:`~pheasant.setup_wizard.Section`, so a new block cannot be
   unreachable from ``pheasant setup``.
2. **Documentation** — every top-level section is mentioned in
   ``docs/configuration.md``.

Like its predecessor this catches the mechanical failure (a section mentioned
nowhere), not prose accuracy.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from pheasant.api.app import LIVE_APPLICABLE_SECTIONS, RETRIEVAL_FIELD_HELP
from pheasant.config.schema import PheasantConfig, RetrievalSettings
from pheasant.setup_wizard import build_sections, schema_default

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGURATION_DOC = REPO_ROOT / "docs" / "configuration.md"

#: Sections the wizard deliberately does not ask about, with the reason. Each
#: entry is a decision, not an oversight — which is the point of listing them
#: here rather than silently excluding them from the check.
WIZARD_EXEMPT = {
    # Created and owned by the memory store, never configured by hand beyond
    # the `memory` block the wizard does cover.
    "sources": "collected interactively, not a settings block",
}


def _top_level_sections() -> list[str]:
    return [field.name for field in dataclasses.fields(PheasantConfig)]


def test_every_config_section_is_reachable_from_pheasant_setup() -> None:
    covered: set[str] = set()
    for section in build_sections():
        covered.update(section.covers)
        # A section that asks about `search.embeddings.*` covers `search`.
        for question in section.questions:
            if not question.key.startswith("__"):
                covered.add(question.key.split(".")[0])

    missing = [
        name for name in _top_level_sections() if name not in covered and name not in WIZARD_EXEMPT
    ]
    assert not missing, (
        "These config sections are not reachable from `pheasant setup`: "
        f"{', '.join(missing)}. Add a Section (or a Question) covering each in "
        "src/pheasant/setup_wizard.py, or add it to WIZARD_EXEMPT here with the "
        "reason it is deliberately not asked about."
    )


def test_every_config_section_is_documented() -> None:
    text = CONFIGURATION_DOC.read_text(encoding="utf-8")
    missing = [name for name in _top_level_sections() if name not in text]
    assert not missing, (
        f"docs/configuration.md does not mention these config sections: "
        f"{', '.join(missing)}. Document each one there."
    )


def test_every_wizard_question_points_at_a_live_schema_field() -> None:
    """A dotted key that no longer resolves is a wizard that crashes on use."""
    for section in build_sections():
        for question in section.questions:
            if question.key.startswith("__"):
                continue
            schema_default(question.key)  # AttributeError on a stale path


def test_every_live_editable_section_is_a_real_config_field() -> None:
    """A typo in the API's section table 404s a section that exists."""
    fields = set(_top_level_sections())
    unknown = [name for name in LIVE_APPLICABLE_SECTIONS if name not in fields]
    assert not unknown, f"LIVE_APPLICABLE_SECTIONS names non-existent sections: {unknown}"


def test_every_config_section_declares_whether_it_applies_live() -> None:
    """A section missing from the table cannot be edited from the UI at all."""
    missing = [
        name
        for name in _top_level_sections()
        if name not in LIVE_APPLICABLE_SECTIONS and name != "sources"
    ]
    assert not missing, (
        f"These sections have no entry in LIVE_APPLICABLE_SECTIONS: {', '.join(missing)}. "
        "Add one saying whether the running process can pick the change up."
    )


def test_every_retrieval_knob_has_help_text() -> None:
    """The UI and `describe_retrieval` both render this; a blank knob is a
    knob nobody can safely change."""
    fields = {field.name for field in dataclasses.fields(RetrievalSettings)}
    missing = fields - set(RETRIEVAL_FIELD_HELP)
    assert not missing, (
        f"RETRIEVAL_FIELD_HELP is missing: {', '.join(sorted(missing))}. "
        "Add one line per knob in src/pheasant/api/app.py."
    )


@pytest.mark.parametrize("extra", ["mcp", "vector", "agent", "wasm", "dev", "analytics", "otel"])
def test_documented_extras_still_exist(extra: str) -> None:
    """The wizard and the docs name these; a renamed extra breaks both."""
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert extra in data["project"]["optional-dependencies"]
