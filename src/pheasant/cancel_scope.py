"""A stop signal that work deep inside one sync item can see.

`sync/cancellation.py` stops a sync *between* items: the commit loop asks
before it hands out the next one. That is enough for a folder of notes and
not for the items that are hours on their own -- an 8,000-page PDF read on
the worker fleet by page range, a web host whose robots.txt asks for a minute
between requests, a remote batch backing off a rolling deploy. Those used to
run to the end after somebody pressed Cancel.

So a running sync carries a :class:`StopSignal` in a context variable, and
the places that *wait* -- a pacing slot, a retry backoff, the next page range,
the next page -- wait on the signal instead of the clock and raise
:class:`Stopped` once it is set. Nothing here is a reason for work to stop
on its own: with no signal bound every function is the call it replaces
(:func:`sleep` is ``time.sleep``, :func:`check` does nothing), which is how a
test, an embedded caller and a worker process behave.

Two properties the rest of the design leans on:

* **A signal is set once and never cleared.** A run that was asked to stop
  stays asked; a new run gets a new signal.
* **Raising is advisory, the commit loop is the authority.** Code that
  catches :class:`Stopped` with a broad ``except`` -- an extractor turning a
  failure into "no text", a fallback reading a page range locally -- cannot
  make a stopped item count, because the commit loop reads the same signal
  (in memory, unthrottled) before it commits anything and stops first. What
  an item computed after the signal was set is discarded, whatever it was.

Thread pools do not inherit a context variable. The engine's executors
already submit through ``copy_context().run``; anything else that fans out
wraps its callable with :func:`propagate`.
"""

from __future__ import annotations

import functools
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

T = TypeVar("T")

#: The longest a :func:`sleep` waits between looks at the clock. A signal is
#: an ``Event``, so a set signal wakes a waiter at once; this only bounds a
#: sleep whose deadline moved (it never does) and keeps a wait interruptible
#: by ``KeyboardInterrupt`` on Windows, where ``Event.wait`` without a
#: timeout is not.
WAIT_SLICE_SECONDS = 1.0


class Stopped(RuntimeError):  # noqa: N818 - named for the event, like SyncCancelled
    """The work this thread is doing was asked to stop."""

    def __init__(self, requested_by: str = "", reason: str = "") -> None:
        self.requested_by = requested_by or "a caller"
        self.reason = reason or ""
        message = f"cancelled by {self.requested_by}"
        super().__init__(f"{message}: {self.reason}" if self.reason else message)


class StopSignal:
    """Set once, by whoever learns the work should stop; read by the work."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self.requested_by = ""
        self.reason = ""

    def set(self, requested_by: str = "", reason: str = "") -> bool:
        """Ask the work to stop. ``True`` if this call was the one that asked."""

        with self._lock:
            if self._event.is_set():
                return False
            self.requested_by = requested_by or "a caller"
            self.reason = reason or ""
            self._event.set()
            return True

    def is_set(self) -> bool:
        return self._event.is_set()

    def error(self) -> Stopped:
        return Stopped(self.requested_by, self.reason)

    def check(self) -> None:
        if self._event.is_set():
            raise self.error()

    def sleep(self, seconds: float) -> None:
        """``time.sleep(seconds)``, raising :class:`Stopped` the moment the signal is set."""

        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._event.wait(min(remaining, WAIT_SLICE_SECONDS)):
                self.check()


_CURRENT: ContextVar[StopSignal | None] = ContextVar("pheasant_stop_signal", default=None)


def current() -> StopSignal | None:
    """The signal of the work this context belongs to, if any."""

    return _CURRENT.get()


@contextmanager
def bound(signal: StopSignal | None) -> Iterator[StopSignal | None]:
    """Run the body as part of the work ``signal`` stops."""

    token = _CURRENT.set(signal)
    try:
        yield signal
    finally:
        _CURRENT.reset(token)


def stopping() -> bool:
    """Whether the current work was asked to stop. Never raises."""

    signal = _CURRENT.get()
    return signal is not None and signal.is_set()


def check() -> None:
    """Raise :class:`Stopped` if the current work was asked to stop."""

    signal = _CURRENT.get()
    if signal is not None:
        signal.check()


def sleep(seconds: float) -> None:
    """Sleep, or stop: ``time.sleep`` outside a sync, interruptible inside one."""

    signal = _CURRENT.get()
    if signal is None:
        time.sleep(max(0.0, float(seconds)))
    else:
        signal.sleep(seconds)


def propagate(fn: Callable[..., T]) -> Callable[..., T]:
    """``fn`` bound to the caller's signal, for a thread that does not inherit it."""

    signal = _CURRENT.get()
    if signal is None:
        return fn

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> T:
        with bound(signal):
            return fn(*args, **kwargs)

    return run
