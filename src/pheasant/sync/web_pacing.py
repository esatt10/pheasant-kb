"""How fast a web collection may ask each site, and how much one run may take.

A web page is somebody else's server. `WebCollectionConnector` fetches pages
on the engine's prepare threads, several at once, and until this it fetched
as fast as those threads went -- which a site that asks for a pause between
requests (``Crawl-delay`` in its robots.txt: system.netsuite.com asks 20 s)
cannot be indexed under at all. :class:`HostPacer` holds one schedule per
host across every thread: a request waits for its host's next slot.

A pause makes a run long -- 2,400 pages at 20 s is over 13 hours, longer than
the sync timeout, and an indexer held that long syncs nothing else -- so a
paced host also gets a **budget per run**: what fits in
:data:`PACED_RUN_SECONDS` of its delay, or ``sources[].sync.max_items_per_run``
when set. A page past the budget is deferred exactly as a page that is not yet
due is: no request, the index keeps what it holds, and it is due on the next
run. A first sync of a large paced site is therefore a series of short runs.

``sources[].sync.min_request_interval_seconds`` overrides robots.txt;
``respect_robots: false`` stops reading it. Disallow rules are not applied
here: every URL in a collection was listed by somebody on purpose.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: A paced host's share of one run: ~55 minutes of its delay.
PACED_RUN_SECONDS = 3300.0

#: The user agent robots.txt groups are matched against, after ``*``.
ROBOTS_AGENT = "pheasant"

#: How long a host's robots.txt answer is trusted before it is read again.
ROBOTS_TTL_SECONDS = 86400.0


def crawl_delay(robots_txt: str, agent: str = ROBOTS_AGENT) -> float:
    """The ``Crawl-delay`` robots.txt asks of ``agent`` (else of ``*``), in seconds."""

    groups: list[tuple[set[str], float | None]] = []
    agents: set[str] = set()
    delay: float | None = None
    in_rules = False
    for raw in robots_txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                groups.append((agents, delay))
                agents, delay, in_rules = set(), None, False
            agents.add(value.lower())
        elif agents:
            in_rules = True
            if key == "crawl-delay":
                try:
                    delay = max(0.0, float(value))
                except ValueError:
                    pass
    if agents:
        groups.append((agents, delay))
    for wanted in (agent.lower(), "*"):
        for names, value in groups:
            if wanted in names and value is not None:
                return value
    return 0.0


class HostPacer:
    """One request schedule per host, shared by every fetching thread of a run."""

    def __init__(
        self,
        source: Any,
        fetch_robots: Callable[[str], str | None],
        *,
        known: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        sync = source.sync
        self.override = getattr(sync, "min_request_interval_seconds", None)
        self.respect_robots = bool(getattr(sync, "respect_robots", True))
        self.max_items = getattr(sync, "max_items_per_run", None)
        self._fetch_robots = fetch_robots
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._delays: dict[str, float] = {}
        self._next: dict[str, float] = {}
        self._requests: dict[str, int] = {}
        self.deferred = 0
        self._wall = wall
        #: robots.txt answers from earlier runs, `{host: {"delay", "checked_at"}}`:
        #: read again at most once a day, so an unchanged collection's beat
        #: still costs a site nothing.
        self._robots: dict[str, dict[str, float]] = {
            str(host): dict(entry)
            for host, entry in (known or {}).items()
            if isinstance(entry, dict)
        }

    def delay_for(self, url: str) -> float:
        """Seconds between requests to ``url``'s host."""

        parsed = urlparse(url)
        host = parsed.netloc.lower()
        with self._lock:
            if host in self._delays:
                return self._delays[host]
        if self.override is not None:
            delay = max(0.0, float(self.override))
        elif self.respect_robots and self._fresh(host):
            delay = float(self._robots[host].get("delay") or 0.0)
        elif self.respect_robots and parsed.scheme in ("http", "https"):
            try:
                text = self._fetch_robots(f"{parsed.scheme}://{parsed.netloc}/robots.txt")
            except Exception as exc:  # noqa: BLE001 - no robots.txt is no delay
                logger.debug("robots.txt for %s unreadable: %s", host, exc)
                text = None
            delay = crawl_delay(text or "")
            with self._lock:
                self._robots[host] = {"delay": delay, "checked_at": self._wall()}
            if delay:
                logger.info("%s asks for %.0f s between requests (robots.txt)", host, delay)
        else:
            delay = 0.0
        with self._lock:
            return self._delays.setdefault(host, delay)

    def _fresh(self, host: str) -> bool:
        entry = self._robots.get(host)
        checked = entry.get("checked_at") if entry else None
        return isinstance(checked, int | float) and self._wall() - checked < ROBOTS_TTL_SECONDS

    def robots_state(self) -> dict[str, dict[str, float]]:
        """What to remember for the next run (the cursor carries it)."""

        with self._lock:
            return {host: dict(entry) for host, entry in self._robots.items()}

    def budget(self, delay: float) -> int | None:
        """Requests one run may make of a host with this delay (``None``: unbounded)."""

        if self.max_items is not None:
            return max(0, int(self.max_items))
        if delay > 0:
            return max(1, int(PACED_RUN_SECONDS // delay))
        return None

    def plan(self, urls: list[str]) -> set[str]:
        """Which of ``urls`` (due, most in need first) this run leaves for the next.

        Decided here, at listing time, rather than as requests are made: pages
        are fetched on several threads at once, and a budget handed out
        first-come would go to whichever thread asked first instead of to
        the pages that have waited longest.
        """

        over: set[str] = set()
        planned: dict[str, int] = {}
        for url in urls:
            host = urlparse(url).netloc.lower()
            limit = self.budget(self.delay_for(url))
            if limit is not None and planned.get(host, 0) >= limit:
                over.add(url)
            else:
                planned[host] = planned.get(host, 0) + 1
        with self._lock:
            self.deferred = len(over)
        return over

    def admit(self, url: str) -> None:
        """Wait for ``url``'s host slot: one request per crawl delay, across threads."""

        delay = self.delay_for(url)
        host = urlparse(url).netloc.lower()
        with self._lock:
            self._requests[host] = self._requests.get(host, 0) + 1
            now = self._clock()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + delay
        wait = slot - self._clock()
        if wait > 0:
            self._sleep(wait)

    def report(self) -> dict[str, Any]:
        """What this run asked of each site, for the checkpoint and the Syncs page."""

        with self._lock:
            return {
                "crawl_delays": {h: d for h, d in self._delays.items() if d > 0},
                "requests": dict(self._requests),
                "budget_deferred": self.deferred,
            }
