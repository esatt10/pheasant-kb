"""The corpus facts a plan is grounded in, kept off the question path.

`PheasantRetriever.capabilities()` runs on every question, before the first
retrieval, and it was most of what a question waited for. Timed inside the
lab's api container (Postgres, 52k chunks): ``ts_stat`` over the full-text
index for the corpus vocabulary, **5.6 s**; a LanceDB row count for the vector
store, **2.9 s**; every other aggregate together under half a second. The
vocabulary was cached against the exact artifact and chunk counts, so while
any source indexed -- most of the time on a live region -- every question
missed the cache and paid the scan again. Three lab questions showed it as 6
to 10 s of a "retrieve" step whose own search arms took a fifth of a second.

None of these facts has to be current to the second: they tell a planner what
the corpus is broadly about, how large it is and which node types it holds. So

* the vocabulary is held per database and served for
  :data:`VOCABULARY_SECONDS`; once stale it is refreshed by one background
  thread while questions keep reading the held copy. Only a database nothing
  has read yet is computed in the caller, and a serving replica reads it at
  boot (`search/keep_warm.py`) so that caller is not a person;
* the vector row count is the resident copy's own live count when one is
  loaded -- exact, and free -- and otherwise a :data:`COUNT_SECONDS` cache, as
  is the graph's node-type roll-up.
"""

from __future__ import annotations

import logging
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: How long a corpus vocabulary is served before a background refresh.
VOCABULARY_SECONDS = 600.0

#: How long a counted figure (vector rows, graph node types) is reused.
COUNT_SECONDS = 60.0

#: The vocabulary a plan reads (`PheasantRetriever.structure`), and so the
#: size keep-warm reads ahead of it.
PLAN_VOCABULARY = 24

_lock = threading.Lock()
_vocabulary: dict[tuple, tuple[float, list[tuple[str, int]]]] = {}
_refreshing: set[tuple] = set()
_counts: dict[tuple, tuple[float, Any, Any]] = {}


def _database_key(state: Any) -> tuple:
    """One key per database, whichever `StateStore` object reads it."""

    path = getattr(state, "path", None)
    if path is not None:
        return ("path", str(path))
    backend = getattr(state, "backend", None)
    dsn = getattr(backend, "dsn", None)
    if dsn:
        return ("dsn", str(dsn))
    return ("object", id(backend if backend is not None else state))


def vocabulary(state: Any, limit: int) -> list[tuple[str, int]]:
    """``corpus_vocabulary(state, limit)``, held and refreshed off the caller."""

    from pheasant.search.sqlite_store import corpus_vocabulary

    key = (_database_key(state), int(limit))
    with _lock:
        held = _vocabulary.get(key)
        stale = held is not None and time.monotonic() - held[0] > VOCABULARY_SECONDS
        start = stale and key not in _refreshing
        if start:
            _refreshing.add(key)
    if held is None:
        terms = list(corpus_vocabulary(state, limit))
        with _lock:
            _vocabulary[key] = (time.monotonic(), terms)
        return list(terms)
    if start:
        threading.Thread(
            target=_refresh,
            args=(key, lambda: corpus_vocabulary(state, limit)),
            name="pheasant-corpus-vocabulary",
            daemon=True,
        ).start()
    return list(held[1])


def _refresh(key: tuple, compute: Callable[[], list[tuple[str, int]]]) -> None:
    try:
        terms = list(compute())
        with _lock:
            _vocabulary[key] = (time.monotonic(), terms)
    except Exception:  # noqa: BLE001 - the held copy keeps serving
        logger.debug("corpus vocabulary refresh failed", exc_info=True)
    finally:
        with _lock:
            _refreshing.discard(key)


def counted(owner: Any, name: str, compute: Callable[[], Any]) -> Any:
    """``compute()`` for ``owner``, reused for :data:`COUNT_SECONDS`.

    Held against a weak reference, so an object at a recycled address is never
    handed another object's figure; an owner that cannot be weakly referenced
    is simply not cached.
    """

    try:
        ref = weakref.ref(owner)
    except TypeError:
        return compute()
    key = (name, id(owner))
    with _lock:
        held = _counts.get(key)
        if held is not None and held[1]() is owner and time.monotonic() - held[0] <= COUNT_SECONDS:
            return held[2]
    value = compute()
    with _lock:
        for stale in [k for k, (_, r, _) in _counts.items() if r() is None]:
            del _counts[stale]
        _counts[key] = (time.monotonic(), ref, value)
    return value


def vector_count(store: Any) -> int:
    """Rows in the vector store: the resident copy's live count when loaded."""

    snapshot = getattr(getattr(store, "_resident", None), "_snapshot", None)
    if snapshot is not None:
        return int(snapshot.live)
    return int(counted(store, "vector_count", store.count))


def forget() -> None:
    """Drop everything held (tests)."""

    with _lock:
        _vocabulary.clear()
        _refreshing.clear()
        _counts.clear()
