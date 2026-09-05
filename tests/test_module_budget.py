"""A ratchet on module size, while the extraction that fixes it waits.

Fifteen files exceed 1,000 lines and the top five hold roughly a fifth of the
codebase. The review's fix is to split them by bounded context — `app.py` into
one router per plane, `schema.py` into one module per section — and it is
explicit that this is much easier *after* the application-service layer is
extracted, because that extraction is what reveals the seams. Splitting them
first would be guessing at the seams and then living with the guess.

So the split is not here. What is here is the property that makes waiting
safe: **the ratchet only moves one way.** These files are the ones every
feature touches, which is exactly why they grow, and a budget that is checked
only when somebody remembers is not a budget.

Two rules, and the second is the one that matters:

* A **new** module may not be born over the threshold. There is no reason for
  one, and it is the cheapest possible moment to say so.
* An **existing** oversized module may not grow. Each is pinned at a ceiling a
  little above where it stands; a change that pushes one past its ceiling has
  to take something out first, or split the file, or make an explicit decision
  to raise the number in this file — which is a line in a diff a reviewer sees,
  rather than a gradual drift nobody does.

The ceilings are headroom, not targets. Lowering one as a module shrinks is
always welcome and never required.
"""

from __future__ import annotations

import pytest

from tests.conftest import REPO_ROOT

SOURCE_ROOT = REPO_ROOT / "src" / "pheasant"

#: Where a *new* module starts needing a reason. Soft in the sense that the
#: table below exempts what already exists; hard for anything new.
THRESHOLD = 800

#: The modules already over the line, each with a ceiling above its current
#: size. Ratchet: lower these as the files shrink, and treat raising one as a
#: decision rather than a formality.
#:
#: `api/app.py` is the one to watch — it is the finding's headline. The
#: service-layer extraction has now taken its first bite (the retrieval and
#: graph operations moved to `services/`, and both surfaces call them), which
#: is why its ceiling and `mcp_server/tools.py`'s are lower than they were.
#: Lowering a ceiling after a split is what makes the ratchet mean anything:
#: the space the extraction freed is not left available for the next feature.
CEILINGS: dict[str, int] = {
    "analytics.py": 1100,
    "api/app.py": 5500,
    "assistant/chat.py": 950,
    "assistant/retrieval.py": 1150,
    "assistant/workflows/agentic.py": 1050,
    "cli.py": 3200,
    "config/schema.py": 2200,
    "evaluation/metrics.py": 1450,
    "evaluation/runner.py": 1500,
    "evaluation/store.py": 950,
    "graph/enrichment.py": 1000,
    "ingestion/taxonomy.py": 1100,
    "jobs.py": 950,
    "mcp_server/server.py": 1300,
    "mcp_server/tools.py": 2050,
    "memory/formation.py": 1150,
    "memory/store.py": 950,
    # Raised from 1050 for the readiness plane's two tables. Deliberate, and
    # the one raise that is not a smell: this file *is* the DDL, so 68 lines
    # of `CREATE TABLE` for `ingest_receipts` and `snapshot_seals` have
    # nowhere better to be — the split this ratchet wants for `schema.py` is
    # one module per section, which is a change to `schema_for` and
    # `columns_of`, not to where a table is declared. The receipt ledger's
    # *code* did move out of `state_store.py` in the same change, which is
    # the split this test is actually asking for.
    "persistence/schema.py": 1100,
    "persistence/state_store.py": 1800,
    "search/vector_store.py": 1400,
    "setup_wizard.py": 2200,
    "sync/engine.py": 2800,
    "sync/queue.py": 1050,
    "telemetry/interactions.py": 1350,
    "tuning/runner.py": 1450,
    "tuning/store.py": 950,
}


def _line_counts() -> dict[str, int]:
    return {
        str(path.relative_to(SOURCE_ROOT)): len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
    }


def test_no_new_module_is_born_oversized() -> None:
    """Anything not already on the list has to fit."""

    offenders = {
        name: count
        for name, count in _line_counts().items()
        if count > THRESHOLD and name not in CEILINGS
    }
    assert not offenders, (
        f"new modules over {THRESHOLD} lines: {offenders}. Split it, or — if it genuinely "
        "belongs as one unit — add it to CEILINGS with a note saying why, so the exception "
        "is a reviewed line rather than a silent one."
    )


@pytest.mark.parametrize("name", sorted(CEILINGS))
def test_an_oversized_module_does_not_grow(name: str) -> None:
    """The ratchet. These are the files every feature touches."""

    counts = _line_counts()
    if name not in counts:
        pytest.skip(f"{name} no longer exists — remove it from CEILINGS")
    ceiling = CEILINGS[name]
    assert counts[name] <= ceiling, (
        f"{name} is {counts[name]} lines, over its {ceiling}-line ceiling. Take something "
        "out, split the file, or raise the ceiling deliberately — a 5,000-line module "
        "cannot be reviewed as a unit, and a diff cannot show you that the block you just "
        "wrote already exists 2,000 lines up."
    )


def test_the_ceilings_are_not_far_above_reality() -> None:
    """A ceiling with a thousand lines of slack is not a ratchet.

    Keeps the table honest as files shrink: an entry that has drifted far above
    its module is one nobody has lowered, and it stops constraining anything
    long before anyone notices.
    """

    counts = _line_counts()
    slack = {
        name: ceiling - counts[name]
        for name, ceiling in CEILINGS.items()
        if name in counts and ceiling - counts[name] > 400
    }
    assert not slack, (
        f"these ceilings have drifted far above their modules: {slack}. Lower them — the "
        "ratchet is only as tight as its loosest entry."
    )


def test_the_headline_module_is_tracked() -> None:
    """`api/app.py` is the finding's headline and the one the service-layer
    extraction exists to move. If it ever leaves this table, that should be
    because it got smaller."""

    assert "api/app.py" in CEILINGS
