"""Memory that improves *every* query, not just the ones it answers (Step 33.8).

Through 33.7 memory was content: something to retrieve. This is the other half
— a record whose payload is not prose to return but a deterministic *retrieval
rule*. It is the cheapest large win available, because it changes ranking on
queries that return no memory at all.

Three kinds, each parsed from the record's own text:

* ``alias``      — ``router -> pheasant-flock, flock``. Expands the query, so
  asking about "the router" finds documents that only ever say
  "pheasant-flock". This is the deterministic form of the learned query
  rewriting the agentic-search literature builds with a policy network; an
  agent that already knows the synonym just writes it down.
* ``preference`` — ``when: deploy, docker -> prefer: docs/, deploy/``. Adds a
  path prior alongside the existing depth / tests / samples ones.
* ``exclusion``  — ``never: vendor/**``. Suppresses candidates outright.

**Scope gating is a security property, not a nicety.** A rule applies only to a
query whose `MemoryPolicy` admits the record's scope, so `session` steering is
confined to that session and `user` steering to that principal. Without it one
agent's note silently re-ranks another's searches — and `org` steering, which
*is* fleet-wide, is the reason that has to be deliberate rather than incidental.

Rule 1 holds by construction: parsing is regex over text an agent already
wrote, no model and no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from pheasant.artifacts.policy import STEERING_KINDS, MemoryPolicy, admits

#: Record kinds that carry a rule rather than an assertion. Canonical
#: definition lives in `policy` (which cannot import this module); re-exported
#: here because this is where readers look for it.
__all__ = ["STEERING_KINDS", "Steering", "load_steering", "load_steering_records", "parse_rule"]

#: Bounds. A steering set is meant to be a handful of deliberate rules; these
#: stop a runaway store from turning every query into a thousand-term
#: expansion, which would cost far more than it could ever return.
MAX_ALIAS_TERMS = 8
MAX_RULES = 64

#: How a rule's trigger is broken up so it can be compared against a tokenized
#: query. This must agree with `search.sqlite_store._query_tokens`, which keeps
#: runs matching ``[A-Za-z][A-Za-z0-9_]{1,}`` and additionally splits each run on
#: ``[_\W]+``. Splitting on whitespace/underscore/hyphen alone — as this did —
#: covers `filewatch daemon` and `pheasant-flock` but leaves every other
#: separator silently dead: `ci/cd`, `fs.watch` and `api:gateway` tokenize to
#: two query terms each while the trigger stayed one unsplittable string, so the
#: rule parsed, loaded, was reported in force by `describe_retrieval`, and never
#: fired. That is the same failure the multi-word fix was written for, one
#: character class short.
_TRIGGER_SPLIT = re.compile(r"[^A-Za-z0-9]+")

_ARROW = re.compile(r"\s*(?:->|→|=>)\s*")
_WHEN = re.compile(
    r"\bwhen\s*:\s*(?P<when>[^\n]*?)\s*(?:->|→|=>)\s*prefer\s*:\s*(?P<prefer>[^\n]+)",
    re.I,
)
_NEVER = re.compile(r"\bnever\s*:\s*(?P<never>[^\n]+)", re.I)


def _trigger_fires(trigger: str, present: set[str]) -> bool:
    """Does `trigger` match this tokenized query?

    True when the trigger survives tokenization whole (`file_service` is itself
    a token), or when **every** part of it is present. All parts, never any:
    matching on one would let a rule about `pheasant-flock` fire on a query that
    merely said `pheasant`.
    """
    if trigger in present:
        return True
    parts = {part for part in _TRIGGER_SPLIT.split(trigger) if len(part) > 1}
    return bool(parts) and parts <= present


def _terms(value: str) -> list[str]:
    """Split a comma/semicolon list, lowercased, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;]", value or ""):
        term = part.strip().strip("`\"'").lower()
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out


@dataclass(frozen=True)
class Steering:
    """Every rule in force for one query, already scope-filtered."""

    #: query term -> extra terms to search for as well.
    aliases: dict[str, list[str]] = field(default_factory=dict)
    #: (trigger terms, preferred path prefixes) pairs.
    preferences: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = ()
    #: path fragments to suppress.
    exclusions: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.aliases or self.preferences or self.exclusions)

    def expand(self, tokens: list[str]) -> list[str]:
        """Query tokens plus any alias expansions they trigger.

        Additive only: the original tokens always survive, so an alias can
        widen a search but never narrow one. Deterministic order, because the
        expansion feeds an FTS `MATCH` string that must be stable.

        A trigger is matched against the *tokenized* query the same way
        `prefers` matches its own, and for the same reason: this looked up
        `aliases[token]` one token at a time, so a multi-word trigger — the
        natural way to write one, `filewatch daemon -> fileService, watcher` —
        could never match any single token and silently never fired. See
        :func:`_trigger_fires` for the matching rule.
        """
        if not self.aliases:
            return tokens
        extra: set[str] = set()
        present = set(tokens)
        for trigger, values in self.aliases.items():
            if not _trigger_fires(trigger, present):
                continue
            for value in values:
                for word in re.split(r"[\s_\-]+", value):
                    word = word.strip().lower()
                    if word and len(word) > 1 and word not in present:
                        extra.add(word)
        return sorted(present | extra)

    def prefers(self, query_tokens: list[str]) -> tuple[str, ...]:
        """Path prefixes preferred for this query, or empty.

        A trigger is compared against the *tokenized* query, so a hyphenated,
        underscored or slash-separated term written naturally in a rule still
        matches: the tokenizer splits `pheasant-flock` into `pheasant` and
        `flock`, and a trigger compared whole would never have fired. See
        :func:`_trigger_fires` for the matching rule.
        """
        wanted = set(query_tokens)
        out: list[str] = []
        for triggers, paths in self.preferences:
            for trigger in triggers:
                if _trigger_fires(trigger, wanted):
                    out.extend(paths)
                    break
        return tuple(dict.fromkeys(out))

    def describe(self) -> dict[str, Any]:
        """What an agent is told is in force, via `describe_retrieval`."""
        return {
            "aliases": {key: list(value) for key, value in sorted(self.aliases.items())},
            "preferences": [
                {"when": list(triggers), "prefer": list(paths)}
                for triggers, paths in self.preferences
            ],
            "exclusions": list(self.exclusions),
        }


def parse_rule(kind: str, text: str) -> tuple[str, Any] | None:
    """One record's text into a rule, or None when it does not parse.

    A malformed rule is **ignored, never raised**: these are written by agents
    into a store that is also read during a sync, and one bad line must not be
    able to fail indexing or a search.
    """
    text = (text or "").strip()
    if not text:
        return None

    if kind == "alias":
        left, _, right = text.partition("\n")[0].partition("->")
        if not right:
            parts = _ARROW.split(text.splitlines()[0], maxsplit=1)
            if len(parts) != 2:
                return None
            left, right = parts
        term = left.strip().strip("`\"'").lower()
        values = _terms(right)[:MAX_ALIAS_TERMS]
        if not term or not values:
            return None
        return ("alias", (term, values))

    if kind == "preference":
        match = _WHEN.search(text)
        if not match:
            return None
        triggers = tuple(_terms(match.group("when")))
        paths = tuple(_terms(match.group("prefer")))
        if not triggers or not paths:
            return None
        return ("preference", (triggers, paths))

    if kind == "exclusion":
        match = _NEVER.search(text)
        if not match:
            return None
        paths = tuple(_terms(match.group("never")))
        return ("exclusion", paths) if paths else None

    return None


def load_steering_records(state: Any) -> list[dict[str, Any]]:
    """Steering records with their text, or `[]`.

    Joined back to `chunks` because the projection stores a record's *metadata*
    and the rule lives in its prose. Restricted to non-`fact` kinds in SQL, so
    a store of ordinary memories costs one indexed lookup that returns nothing
    rather than a scan of every record's body.

    The chunks are re-assembled **in `chunk_index` order, here**, rather than by
    `GROUP_CONCAT`. SQLite does not guarantee the order an aggregate sees its
    input, so a rule long enough to span two chunks could be concatenated back
    to front — parsing differently, or not at all, between two runs over
    identical content. That is a determinism break (rule 1) on a path whose
    whole job is to be reproducible, and it fails *silently*: the rule simply
    stops being in force.
    """
    try:
        rows = state.rows(
            "SELECT m.record_id, m.scope, m.subject, m.kind, m.valid_from, m.valid_until, "
            "c.chunk_index, c.text "
            "FROM memory_records m JOIN chunks c ON c.artifact_id = m.artifact_id "
            "WHERE m.kind <> 'fact' ORDER BY m.record_id, c.chunk_index"
        )
    except Exception:  # pragma: no cover - state store older than 33.5
        return []
    records: dict[str, dict[str, Any]] = {}
    pieces: dict[str, list[str]] = {}
    for row in rows:
        record = dict(row)
        record_id = str(record.get("record_id") or "")
        text = str(record.pop("text", "") or "")
        record.pop("chunk_index", None)
        records.setdefault(record_id, record)
        pieces.setdefault(record_id, []).append(text)
    for record_id, record in records.items():
        record["text"] = " ".join(pieces[record_id])
    return list(records.values())


def load_steering(
    records: list[dict[str, Any]],
    policy: MemoryPolicy,
    *,
    now: str,
    enabled: bool = False,
    kinds: tuple[str, ...] | None = None,
) -> Steering:
    """The rules a given query may use, or an empty `Steering`.

    `records` are `memory_records` rows carrying `kind` and `text`. Each is put
    through the *same* `admits` predicate that decides whether the record could
    be retrieved at all — so a rule from a corrected, expired or out-of-scope
    record steers nothing, and the two can never disagree about what a query is
    allowed to see.

    The one part of the policy deliberately **not** applied is `mode`.
    `memory: "off"` is a statement about what appears in the results, not about
    which rules are valid: a caller who does not want remembered *content*
    still wants the region's remembered *vocabulary*, and in fact steering's
    whole value is that it improves queries returning no memory at all. Scope,
    subject and validity all still apply.

    `kinds` narrows which rule kinds are in force. `None` -- the default and
    every production call site -- means all of them, so retrieval behaves
    exactly as it did before this argument existed. The evaluation plane passes
    an explicit subset (or `()`) to build its ablation matrix: measuring what
    *alias* rules alone contribute requires a run in which preference and
    exclusion rules do not fire, and a paired ablation whose two sides differ
    in more than the intervention measures neither of them.
    """
    if not enabled or not records:
        return Steering()
    allowed = STEERING_KINDS if kinds is None else tuple(kinds)
    if not allowed:
        return Steering()
    # `include_rules` is a statement about what is *returned as content* —
    # steering records are machinery, so they stay out of result lists by
    # default. Here we are deciding which rules are in force, which is the one
    # place that must see them, so the content filter is lifted while scope,
    # subject and validity gating stay exactly as they were.
    rule_policy = replace(policy, mode="auto", include_rules=True)

    aliases: dict[str, list[str]] = {}
    preferences: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    exclusions: list[str] = []

    for record in sorted(records, key=lambda r: str(r.get("record_id") or ""))[:MAX_RULES]:
        kind = str(record.get("kind") or "")
        if kind not in STEERING_KINDS or kind not in allowed:
            continue
        if not admits(rule_policy, record, now=now):
            continue
        parsed = parse_rule(kind, str(record.get("text") or ""))
        if parsed is None:
            continue
        name, value = parsed
        if name == "alias":
            term, values = value
            aliases.setdefault(term, [])
            for item in values:
                if item not in aliases[term]:
                    aliases[term].append(item)
        elif name == "preference":
            preferences.append(value)
        else:
            exclusions.extend(value)

    return Steering(
        aliases=aliases,
        preferences=tuple(preferences),
        exclusions=tuple(dict.fromkeys(exclusions)),
    )
