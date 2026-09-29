"""Choose a graph checkpoint interval from measured save cost and MTBF."""

from __future__ import annotations

import math


def optimal_checkpoint_interval(
    save_seconds: float,
    *,
    mtbf_seconds: float,
    floor_seconds: float,
    ceiling_seconds: float,
) -> float:
    """How long to wait between graph checkpoints — Young's formula.

    The previous rule was ``max(60s, last_save * 10)``: space checkpoints at
    ten times what one costs, holding overhead near **10%**. That is stable
    and far too expensive. Checkpointing is a solved problem in HPC and ML
    training, and the standard result is Young's (later Daly's) formula:

        T_opt = sqrt(2 * C * MTBF)

    where ``C`` is what a checkpoint costs and ``MTBF`` is the expected time
    between interruptions. It minimizes *total* expected time — the sum of
    checkpoint overhead and the work redone after a crash — rather than
    either one alone, which is why it is not simply "checkpoint less often".

    On pheasant's measured save costs, against a 24h MTBF:

        corpus        C        old interval / overhead    Young / overhead
        2,000        0.13s      60s   /  0.2%             150s  / 0.1%
        20,000       1.32s      60s   /  2.2%             478s  / 0.3%
        100,000      6.71s      67s  / 10.0%            1077s  / 0.6%
        250,000     19.91s     199s  / 10.0%            1855s  / 1.1%

    An order of magnitude less overhead, and the cost is bounded rework: at
    most one interval of indexing is lost, which the ceiling caps at 30
    minutes by default. On a sync that runs for hours at those sizes, that is
    the right trade — and it is the trade the formula is choosing, not a
    guess.

    Degrades sensibly: with no measured save yet (the first checkpoint of a
    fresh graph) or no MTBF configured, it returns the floor, which is the
    pre-35.4 behaviour.
    """

    if save_seconds <= 0 or mtbf_seconds <= 0:
        return floor_seconds
    interval = math.sqrt(2.0 * save_seconds * mtbf_seconds)
    if ceiling_seconds > 0:
        interval = min(interval, ceiling_seconds)
    return max(floor_seconds, interval)
