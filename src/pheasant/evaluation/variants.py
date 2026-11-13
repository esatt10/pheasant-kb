"""The baseline and ablation matrix.

An ablation result is valid only when the intervention is the *only* difference
between the paired runs. That sentence is the whole module: every variant here
names its baseline explicitly, and every variant differs from that baseline in
exactly one declared thing.

The default matrix is the specification's:

======  ==============  =====  ==========  =========  ==========================
Id      Memory results  Alias  Preference  Exclusion  What it isolates
======  ==============  =====  ==========  =========  ==========================
``B0``  off             no     no          no         the corpus alone
``B1``  auto            no     no          no         memory as *content*
``B2``  off             yes    no          no         vocabulary adaptation
``B3``  off             no     yes         no         preferred-source ranking
``B4``  off             no     no          yes        noise suppression
``B5``  auto            yes    yes         yes        the memory system entire
``B6``  auto            yes    yes         yes        B5 plus candidate shadow
======  ==============  =====  ==========  =========  ==========================

Two design notes worth stating where they will be read.

**B2-B4 hold memory content off.** A steering rule is measured by what it does
to *corpus* ranking, and running it with memory passages on would let a
retrieved memory record take a slot and get counted as the rule's doing. The
rule and the passage are separate interventions with separate rows.

**B6 is the only variant that can contain something not yet live**, and it is
never a production configuration -- shadow validation runs it, compares it to
B5, and a candidate reaches retrieval only if that comparison passes the gates.
A candidate that improved its own originating query and nothing else fails on
the holdout, which is the loop this exists to close.

Leave-one-out variants (per record, per cluster) are generated on demand rather
than enumerated, because their count is the store's size.
"""

from __future__ import annotations

from typing import Any

from pheasant.evaluation.contracts import Variant

#: The steering kinds, in the order the matrix walks them.
STEERING_KINDS: tuple[str, ...] = ("alias", "preference", "exclusion")

#: The corpus-only reference every attribution subtracts from.
CORPUS_BASELINE = "B0"


def default_matrix(*, candidate_ids: tuple[str, ...] = ()) -> list[Variant]:
    """The seven-variant default. ``B6`` is omitted when nothing is proposed."""

    matrix = [
        Variant(
            variant_id="B0",
            label="corpus baseline",
            memory_results="off",
            steering_kinds=(),
            description="No memory passages and no steering rules. The reference point.",
        ),
        Variant(
            variant_id="B1",
            label="artifact knowledge",
            memory_results="auto",
            steering_kinds=(),
            baseline_variant_id=CORPUS_BASELINE,
            description="Memory records compete for slots as ordinary passages.",
        ),
        Variant(
            variant_id="B2",
            label="alias steering",
            memory_results="off",
            steering_kinds=("alias",),
            baseline_variant_id=CORPUS_BASELINE,
            description="Vocabulary adaptation only: query expansion from alias rules.",
        ),
        Variant(
            variant_id="B3",
            label="preference steering",
            memory_results="off",
            steering_kinds=("preference",),
            baseline_variant_id=CORPUS_BASELINE,
            description="Preferred-path ranking only.",
        ),
        Variant(
            variant_id="B4",
            label="exclusion steering",
            memory_results="off",
            steering_kinds=("exclusion",),
            baseline_variant_id=CORPUS_BASELINE,
            description="Noise suppression only.",
        ),
        Variant(
            variant_id="B5",
            label="artifact knowledge and steering",
            memory_results="auto",
            steering_kinds=STEERING_KINDS,
            baseline_variant_id=CORPUS_BASELINE,
            description="Everything the memory system currently does.",
        ),
    ]
    if candidate_ids:
        matrix.append(
            Variant(
                variant_id="B6",
                label="candidate shadow",
                memory_results="auto",
                steering_kinds=STEERING_KINDS,
                baseline_variant_id="B5",
                candidate_ids=tuple(sorted(candidate_ids)),
                description=(
                    "B5 plus the proposed interventions. Never a production "
                    "configuration; it exists to be compared with B5."
                ),
            )
        )
    return matrix


def leave_one_out(base: Variant, record_id: str) -> Variant:
    """``base`` with one record held out, paired against ``base``.

    Per-record attribution: the delta between these two is what that one
    record is worth on this cohort. Its baseline is ``base`` rather than the
    corpus, because the question is "what does this record add to the system
    as configured", not "what does it add to nothing".
    """

    return Variant(
        variant_id=f"{base.variant_id}-loo-{record_id}",
        label=f"{base.label} without {record_id}",
        memory_results=base.memory_results,
        steering_kinds=base.steering_kinds,
        tiers=base.tiers,
        baseline_variant_id=base.variant_id,
        excluded_record_ids=(record_id,),
        description=f"{base.description} One record ({record_id}) held out.",
    )


def leave_one_cluster_out(base: Variant, cluster_id: str, record_ids: tuple[str, ...]) -> Variant:
    """``base`` with a compaction cluster's members held out."""

    return Variant(
        variant_id=f"{base.variant_id}-loco-{cluster_id}",
        label=f"{base.label} without cluster {cluster_id}",
        memory_results=base.memory_results,
        steering_kinds=base.steering_kinds,
        tiers=base.tiers,
        baseline_variant_id=base.variant_id,
        excluded_record_ids=tuple(sorted(record_ids)),
        description=f"{base.description} Cluster {cluster_id} held out.",
    )


def tier_comparison(base: Variant) -> Variant:
    """``base`` widened to hot **and** cold memory tiers.

    The one ablation that widens rather than narrows. Cold records are subsumed
    or superseded history, so this answers "would including history have
    helped" -- which is a real question and a different one from whether the
    hot set is good.
    """

    return Variant(
        variant_id=f"{base.variant_id}-cold",
        label=f"{base.label} + cold tier",
        memory_results=base.memory_results,
        steering_kinds=base.steering_kinds,
        tiers=("hot", "cold"),
        baseline_variant_id=base.variant_id,
        description=f"{base.description} Cold tier included.",
    )


def selected_matrix(settings: Any, *, candidate_ids: tuple[str, ...] = ()) -> list[Variant]:
    """The matrix trimmed to what ``evaluation.variants`` enables.

    ``B0`` is never removable. Every attribution metric in the report is a
    paired difference against it, and a matrix without it produces treatment
    numbers with nothing to subtract -- which would be published as absolute
    scores and read as accuracy.
    """

    if settings is None:
        return default_matrix(candidate_ids=candidate_ids)
    wanted = {
        "B0": True,
        "B1": bool(getattr(settings, "memory_content", True)),
        "B2": bool(getattr(settings, "alias_only", True)),
        "B3": bool(getattr(settings, "preference_only", True)),
        "B4": bool(getattr(settings, "exclusion_only", True)),
        "B5": bool(getattr(settings, "full_memory", True)),
        "B6": bool(getattr(settings, "candidate_shadow", True)) and bool(candidate_ids),
    }
    return [
        v for v in default_matrix(candidate_ids=candidate_ids) if wanted.get(v.variant_id, True)
    ]
