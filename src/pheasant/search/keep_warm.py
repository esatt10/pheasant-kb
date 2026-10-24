"""Keep a serving replica's first-query path warm.

The first question after a quiet spell was the slow one: 12.7 s to retrieve on
the lab after 46 idle minutes, against 0.8 s for the next. Everything a search
touches had gone cold at once -- the TLS session to the embedding provider,
Postgres's pooled connections and pages, the graph service's connection --
and the cold embedding cost the most, twice over: it ran past the 1.0 s
query-embedding timeout, so the vector arm returned nothing, the answer loop
judged three text passages insufficient, and it replanned into a second
retrieval.

So a serving replica keeps the path warm instead of paying for it on a
person's first question. Every :data:`INTERVAL_SECONDS` it

* asks the embedding provider for its model list (``ping``) -- a token-free
  request that keeps a kept-alive connection open in the shared pool any
  request thread then draws from; and
* runs one canned hybrid search through the real stack, with usage tracking,
  stage sampling and the retrieval counters off, so it moves no salience, no
  ledger row and no metric a tuning batch reads. Its query embedding is cached
  after the first beat, so it costs the provider nothing either; and
* reads the corpus vocabulary a plan is grounded in (`search/corpus_shape.py`),
  once at start and then whenever it goes stale, so the 5.6 s ``ts_stat`` scan
  behind it is never a person's wait.

Deliberately not a substitute for the request path's own resilience: a beat
that fails is logged at debug and the next one tries again.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Any

from pheasant.search import corpus_shape

logger = logging.getLogger(__name__)

#: Between beats. Providers close an idle keep-alive connection after a minute
#: or two; half a minute keeps one open with room to spare.
INTERVAL_SECONDS = 30.0

#: The canned query. Any ordinary words do: what it warms is the path, not an
#: answer, and nothing it returns is shown to anyone.
WARM_QUERY = "knowledge base overview"


def quiet_copy(search: Any) -> Any:
    """``search`` with every side effect of a search turned off."""

    quiet = copy.copy(search)
    quiet.usage_tracking = False
    quiet.stage_sample_rate = 0.0
    quiet.observed = False
    return quiet


def start(
    search: Any, knowledge_base: str, *, interval: float = INTERVAL_SECONDS
) -> threading.Thread:
    """Begin warming ``search``'s path for ``knowledge_base`` in the background."""

    quiet = quiet_copy(search)
    embedder = getattr(getattr(search, "vector", None), "embedder", None)

    def vocabulary() -> None:
        state = getattr(getattr(search, "store", None), "state", None)
        if state is not None:
            try:
                corpus_shape.vocabulary(state, corpus_shape.PLAN_VOCABULARY)
            except Exception:  # noqa: BLE001 - as below
                logger.debug("keep-warm: corpus vocabulary failed", exc_info=True)

    def beat() -> None:
        vocabulary()
        while True:
            time.sleep(interval)
            vocabulary()
            ping = getattr(embedder, "ping", None)
            if callable(ping):
                try:
                    ping()
                except Exception:  # noqa: BLE001 - a missed beat costs one window
                    logger.debug("keep-warm: provider ping failed", exc_info=True)
            try:
                quiet.search_context(knowledge_base, WARM_QUERY, mode="hybrid", max_results=5)
            except Exception:  # noqa: BLE001 - as above
                logger.debug("keep-warm: canned search failed", exc_info=True)

    thread = threading.Thread(target=beat, name="pheasant-keep-warm", daemon=True)
    thread.start()
    return thread
