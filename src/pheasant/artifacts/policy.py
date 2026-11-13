"""Query-time memory policy (Step 33.6).

Before this, memory was invisible at retrieval time. A memory chunk looked like
any other Markdown note, the only lever was `exclude_sources=["<name>"]` — MCP
only, and only if the caller already knew the source's configured name — and a
*superseded* record stayed retrievable until the batch consolidation pass
archived it and a full re-sync ran. That last one is the stale-fact error the
agent-memory literature names as the primary defect: the region knows the fact
was corrected and serves the old one anyway.

`MemoryPolicy` is the one knob, spelled identically on every protocol. It is
optional everywhere and its default reproduces the pre-33.6 behavior, except
that a corrected record is no longer returned — which is a bug fix, not a
policy choice.

**One rule, two renderings.** Validity is filtered in SQL for the text arm
(post-filtering a globally-ranked page can return nothing from a narrow slice
while its rows sit just past the cut) and in Python for the vector and graph
arms. Those two have to agree exactly, and they used to be two independent
transcriptions of the same rule that a parity test compared afterwards.

They are now one definition with two renderers. :func:`clauses` builds the rule
as a list of :class:`Clause` objects, each carrying its SQL fragment *and* its
Python predicate in one place; :func:`sql_predicate` joins the first,
:func:`admits` evaluates the second. The value is visible in the corners: the
"an empty string means unset" rule needs `NULLIF(tier,'')` in SQL and
`or "hot"` in Python, and getting one without the other silently excluded rows
from one arm only. Both spellings now sit on adjacent lines of the same object,
where nobody can change one without seeing the other.

`tests/test_memory_policy.py` still compares the two over a matrix of records
— not as the thing keeping them in agreement, but because a renderer can be
wrong on its own.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: `auto`   — memory participates like any other source (the default).
#: `off`    — no memory in the results at all.
#: `only`   — memory and nothing else.
#: `prefer` — memory participates, and is guaranteed a share of the slots it
#:            would otherwise have been truncated out of.
#: Record kinds that carry a retrieval *rule* rather than an assertion. Defined
#: here rather than in `steering.py` because `admits`/`sql_predicate` need it and
#: `steering` already imports from this module — `steering.STEERING_KINDS` re-exports
#: it so the name stays where readers of the steering code expect to find it.
STEERING_KINDS = ("alias", "preference", "exclusion")

VALID_MODES = ("auto", "off", "only", "prefer")
DEFAULT_MODE = "auto"

#: Fraction of `max_results` reserved for memory under `prefer`. Half: enough
#: that remembered context cannot be crowded out by a large corpus, not so much
#: that the corpus stops being the answer.
PREFER_SHARE = 0.5


@dataclass(frozen=True)
class ArtifactPolicy:
    """How one query treats the region's agent memory."""

    mode: str = DEFAULT_MODE
    scopes: tuple[str, ...] | None = None
    subject: str | None = None
    #: Drop records a later record has corrected. On by default: serving a
    #: fact the region *knows* was superseded is never what the caller wanted.
    current_only: bool = True
    #: Point-in-time recall — "what did we believe then". Implies the validity
    #: window is evaluated at that instant instead of now, which is what makes
    #: a superseded record visible again rather than merely unfiltered.
    as_of: str | None = None
    #: Cap on how many of the returned slots memory may occupy.
    max_results: int | None = None
    #: Return steering records (`alias`/`preference`/`exclusion`) as content
    #: too. Off by default: a steering record is retrieval *machinery*, not an
    #: assertion, and returning it answers a question nobody asked. Measured
    #: live on the vscode corpus — the alias rule `filewatch daemon ->
    #: fileService, watcher` took **rank 1** for "where is the file service
    #: implemented", pushing the real `fileService.ts` to rank 5 and dropping
    #: corpus MRR from 0.462 to 0.335. It also hands an agent rule syntax
    #: dressed as retrieved knowledge, which is the cheap half of the
    #: memory-control-flow surface 33.6 set out to contain. The rules stay
    #: fully in force and fully inspectable via `describe_retrieval`'s memory
    #: block and `GET /memory`; this governs only whether they are *returned
    #: as passages*. Set true to see them in results anyway.
    include_rules: bool = False
    #: Which tiers (`hot`, `cold`) this query may see (Phase 3). `None` (the
    #: default) defers to :func:`allowed_tiers`: hot only, unless `as_of` is
    #: set or `current_only` is off, in which case cold is included too —
    #: the same signal that already widens the validity window widens the
    #: tier set, since a subsumed-but-valid record and a retained-but-
    #: superseded one are both "history a normal query does not want but an
    #: explicit one can ask for". An explicit value here always wins.
    tiers: tuple[str, ...] | None = None

    @property
    def is_default(self) -> bool:
        """True when this policy asks for nothing the pre-33.6 code did not do.

        Used to keep a region without memory on exactly its old code path.

        **Not** the same question as "does this policy filter anything" — the
        default policy filters plenty (`current_only` drops corrected records,
        `include_rules=False` drops steering rules, and — Phase 3 — an unset
        `tiers` still means hot-only), which is why those are deliberately
        absent from the check below. Ask :func:`may_filter` for that;
        conflating the two is what caused the vector/graph under-fetch.
        """
        return (
            self.mode == DEFAULT_MODE
            and not self.scopes
            and not self.subject
            and self.as_of is None
            and self.max_results is None
            and self.tiers is None
        )

    @classmethod
    def parse(cls, value: str | Mapping[str, Any] | MemoryPolicy | None) -> MemoryPolicy:
        """Accept the shorthand string, a mapping, or an existing policy.

        `"off"` is the common case and deserves to be one word; the mapping
        form exists for the rest. An unknown mode falls back to the default
        rather than raising: a retrieval hint is not worth failing a search
        over, and the same forgiving treatment is what `mode` already gets in
        `HybridSearch.search_context`.
        """
        if value is None:
            return cls()
        if isinstance(value, MemoryPolicy):
            return value
        if isinstance(value, str):
            mode = value.strip().lower()
            return cls(mode=mode if mode in VALID_MODES else DEFAULT_MODE)
        if not isinstance(value, Mapping):
            return cls()

        mode = str(value.get("mode") or DEFAULT_MODE).strip().lower()
        raw_scopes = value.get("scopes")
        if isinstance(raw_scopes, str):
            raw_scopes = [raw_scopes]
        scopes = tuple(str(scope) for scope in raw_scopes) if raw_scopes else None
        raw_tiers = value.get("tiers")
        if isinstance(raw_tiers, str):
            raw_tiers = [raw_tiers]
        tiers = tuple(str(tier) for tier in raw_tiers) if raw_tiers else None
        max_results = value.get("max_results")
        return cls(
            mode=mode if mode in VALID_MODES else DEFAULT_MODE,
            scopes=scopes,
            subject=(str(value["subject"]) if value.get("subject") else None),
            current_only=bool(value.get("current_only", True)),
            as_of=(str(value["as_of"]) if value.get("as_of") else None),
            max_results=(int(max_results) if max_results is not None else None),
            include_rules=bool(value.get("include_rules", False)),
            tiers=tiers,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "scopes": list(self.scopes) if self.scopes else None,
            "subject": self.subject,
            "current_only": self.current_only,
            "as_of": self.as_of,
            "max_results": self.max_results,
            "include_rules": self.include_rules,
            "tiers": list(self.tiers) if self.tiers else None,
        }


def utc_now_iso() -> str:
    """The instant validity is evaluated at, in the format records store."""
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


#: The full tier set (Phase 3). `hot` is every result set a query would
#: normally see; `cold` is a subsumed-but-valid or retained-but-superseded
#: record — reachable, never returned by default.
ALL_TIERS = ("hot", "cold")
DEFAULT_TIERS = ("hot",)


def allowed_tiers(policy: MemoryPolicy) -> tuple[str, ...]:
    """Which tiers a policy may see (Phase 3). The single owner of this
    rule — :func:`admits` and :func:`sql_predicate` both call it rather than
    each inlining the logic, the same arrangement :func:`validity_instant`
    has for the validity window.

    An explicit `policy.tiers` always wins. Otherwise: `as_of` or
    `current_only=False` widens to every tier — the same signal that
    already asks for corrected history is the signal that asks for demoted
    history too, since both are "not what a normal query wants, but
    reachable on request." A plain default policy sees `hot` only.
    """
    if policy.tiers:
        return tuple(policy.tiers)
    if policy.as_of or not policy.current_only:
        return ALL_TIERS
    return DEFAULT_TIERS


def validity_instant(policy: MemoryPolicy, now: str) -> str | None:
    """When to evaluate a record's validity window, or None for no filter.

    `as_of` always wins — asking what was believed on a date is meaningless if
    the answer is silently clamped to what is current. Otherwise the window is
    applied only when `current_only` is on, so switching it off returns the
    full history including corrected records.
    """
    if policy.as_of:
        return policy.as_of
    return now if policy.current_only else None


@dataclass(frozen=True)
class Clause:
    """One condition of the validity rule, in both of its renderings.

    ``sql`` returns ``(fragment, params)`` for a given table alias; ``holds``
    answers the same question about an in-memory record. They are fields of one
    object rather than lines in two functions because the corners are where
    these disagree, and a corner is only safe when both spellings are in front
    of the reader at once.
    """

    name: str
    sql: Callable[[str], tuple[str, list[Any]]]
    holds: Callable[[Mapping[str, Any]], bool]


def clauses(policy: MemoryPolicy, *, now: str) -> list[Clause]:
    """The validity rule, once.

    Every condition that decides whether a *memory record* qualifies. The
    record-vs-not-a-record and mode wrapping live in the two renderers below,
    because they are about what surrounds the rule rather than part of it.
    """

    built: list[Clause] = []

    if not policy.include_rules:
        # Named uniquely, not `placeholders`: every clause below builds one of
        # these, and a lambda closes over the *name*. Sharing it made the
        # steering fragment render with the scope clause's placeholder count —
        # a binding-count error at query time, caught by the parity test that
        # this consolidation was supposed to make redundant. It is not
        # redundant; it checks the renderers, which can each be wrong alone.
        steering_placeholders = ",".join("?" for _ in STEERING_KINDS)
        built.append(
            Clause(
                name="steering",
                sql=lambda alias: (
                    f"COALESCE({alias}.kind, '') NOT IN ({steering_placeholders})",
                    list(STEERING_KINDS),
                ),
                holds=lambda record: str(record.get("kind") or "") not in STEERING_KINDS,
            )
        )

    tiers = allowed_tiers(policy)
    tier_placeholders = ",".join("?" for _ in tiers)
    built.append(
        Clause(
            name="tier",
            # `NULLIF(tier,'')` before `COALESCE`, not `COALESCE` alone: the
            # Python side's `or "hot"` treats an empty string as unset (falsy)
            # and substitutes "hot", but plain `COALESCE` only replaces NULL —
            # an empty string passes through as itself and would then fail to
            # match 'hot' in the IN-list, silently excluding the row. NULLIF
            # converts '' to NULL first so COALESCE actually catches it.
            sql=lambda alias: (
                f"COALESCE(NULLIF({alias}.tier, ''), 'hot') IN ({tier_placeholders})",
                list(tiers),
            ),
            holds=lambda record: str(record.get("tier") or "hot") in tiers,
        )
    )

    if policy.scopes:
        scopes = tuple(policy.scopes)
        scope_placeholders = ",".join("?" for _ in scopes)
        built.append(
            Clause(
                name="scope",
                sql=lambda alias: (f"{alias}.scope IN ({scope_placeholders})", list(scopes)),
                holds=lambda record: str(record.get("scope") or "") in scopes,
            )
        )

    needle = (policy.subject or "").strip().lower()
    if needle:
        built.append(
            Clause(
                name="subject",
                sql=lambda alias: (
                    f"LOWER(COALESCE({alias}.subject, '')) LIKE ?",
                    [f"%{needle}%"],
                ),
                holds=lambda record: needle in str(record.get("subject") or "").lower(),
            )
        )

    instant = validity_instant(policy, now)
    if instant is not None:
        # COALESCE on both sides so an empty string means "unset", exactly as
        # the Python branch reads it. Without it, `'' > instant` is false in
        # SQL and true-ish in Python and the two halves disagree on a corner.
        built.append(
            Clause(
                name="valid_from",
                sql=lambda alias: (
                    f"(COALESCE({alias}.valid_from, '') = '' OR {alias}.valid_from <= ?)",
                    [instant],
                ),
                holds=lambda record: (
                    not (
                        str(record.get("valid_from") or "")
                        and str(record.get("valid_from") or "") > instant
                    )
                ),
            )
        )
        built.append(
            Clause(
                name="valid_until",
                sql=lambda alias: (
                    f"(COALESCE({alias}.valid_until, '') = '' OR {alias}.valid_until > ?)",
                    [instant],
                ),
                holds=lambda record: (
                    not (
                        str(record.get("valid_until") or "")
                        and str(record.get("valid_until") or "") <= instant
                    )
                ),
            )
        )

    return built


def admits(policy: MemoryPolicy, record: Mapping[str, Any] | None, *, now: str) -> bool:
    """Does this policy allow a hit through? `record=None` means "not a memory".

    The Python renderer of :func:`clauses`; :func:`sql_predicate` is the other.
    """
    if record is None:
        return policy.mode != "only"
    if policy.mode == "off":
        return False
    return all(clause.holds(record) for clause in clauses(policy, now=now))


def unique_records(index: Mapping[str, Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """One entry per record from a `load_memory_index` map.

    The index deliberately holds two keys per record (artifact id and
    `source_id\\0relative_path`), so its values repeat.
    """
    seen: set[str] = set()
    out: list[Mapping[str, Any]] = []
    for record in index.values():
        key = str(record.get("record_id") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


def may_filter(policy: MemoryPolicy, index: Mapping[str, Mapping[str, Any]], *, now: str) -> bool:
    """Could this policy drop a hit, given the records that actually exist?

    Drives the over-fetch decision in `HybridSearch.search_context`, which used
    to ask `policy.is_default` — a different question, answered wrong. The
    default policy is *not* inert: `current_only=True` drops corrected records
    and `include_rules=False` drops steering rules. So on the default path the
    over-fetch was skipped, the vector and graph arms were fetched at exactly
    `max_results`, and were then filtered in Python *after* that truncation —
    returning a short page while the hits that should have filled it sat just
    past the cut. The text arm was never affected: its predicate is pushed into
    SQL ahead of `LIMIT`.

    Answered against the loaded index rather than against the policy alone, so a
    region whose memory happens to hold nothing droppable still pays nothing.
    """
    if not index:
        return False
    if not policy.is_default:
        return True
    return any(not admits(policy, record, now=now) for record in unique_records(index))


def sql_predicate(
    policy: MemoryPolicy, *, now: str, alias: str = "memory_records"
) -> tuple[str, list[Any]]:
    """The SQL renderer of :func:`clauses`, for a LEFT JOIN against `memory_records`.

    Returns `(condition, params)`. A row with `record_id IS NULL` is not a
    memory record; under every mode but `only` it passes untouched, which is
    what keeps a scope or subject filter from also narrowing the corpus.
    """
    if policy.mode == "off":
        return f"{alias}.record_id IS NULL", []

    fragments: list[str] = []
    params: list[Any] = []
    for clause in clauses(policy, now=now):
        fragment, clause_params = clause.sql(alias)
        fragments.append(fragment)
        params.extend(clause_params)

    qualifies = " AND ".join(fragments) if fragments else "1=1"
    if policy.mode == "only":
        return f"({alias}.record_id IS NOT NULL AND {qualifies})", params
    return f"({alias}.record_id IS NULL OR ({qualifies}))", params


#: Columns the projection exposes to retrieval. Selected explicitly rather than
#: `SELECT *` so adding a column (33.9's counters) cannot silently widen what a
#: search result reports about a memory.
INDEX_COLUMNS = (
    "artifact_id, source_id, record_id, scope, subject, kind, asserted_at, valid_from, "
    "valid_until, tier"
)


#: Separator for the composite `(source_id, relative_path)` key. NUL cannot
#: occur in either half, so the two can never be confused for one another.
_PATH_KEY = "\x00"


def load_memory_index(state: Any) -> dict[str, dict[str, Any]]:
    """Every memory record, keyed by each identity a search hit might carry.

    One small query per search. The table holds one row per memory record — a
    handful in practice, and bounded outright by `memory.max_records` — so
    loading it is cheaper than joining it three times, and it lets the vector
    and graph arms share the text arm's rule instead of approximating it.

    Two keys per record, because hits do not agree on how they name things: a
    chunk or artifact hit carries the **artifact id**, while a graph hit is a
    *chunk node* whose id is `chunk:…` and whose `relative_path` is null. Keying
    only by artifact id silently admitted every graph-arm hit — a corrected
    record came back through the graph while being correctly filtered out of
    the text arm.
    """
    # A store need not have one: `HybridSearch` accepts any object exposing
    # `search`, and has always been constructed that way in tests and by
    # callers that only want the graph arm. No state means no memory, which is
    # a perfectly ordinary answer rather than an error.
    if state is None:
        return {}
    try:
        rows = state.rows(f"SELECT {INDEX_COLUMNS} FROM memory_records")
    except Exception:  # pragma: no cover - a state store older than 33.5
        return {}
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = dict(row)
        artifact_id = str(record.get("artifact_id") or "")
        index[artifact_id] = record
        relative_path = relative_path_of(artifact_id)
        source_id = record.get("source_id") or ""
        if relative_path:
            index[f"{source_id}{_PATH_KEY}{relative_path}"] = record
    return index


def relative_path_of(stable_id: str) -> str:
    """The relative path inside an artifact or chunk stable id, or `""`.

    The grammars are contracts (`docs/graph_model.md`, CLAUDE.md rule 3):

        file:{source}:{relpath}:branch={b}
        chunk:{source}:{relpath}:sha256={hash}:chunk={index}

    Both put the path third, so the path is what sits between the second colon
    and the trailing `:branch=` / `:sha256=` marker. Split on those markers
    rather than on colon count, since a path may legitimately contain a colon.
    """
    parts = stable_id.split(":", 2)
    if len(parts) < 3:
        return ""
    tail = parts[2]
    for marker in (":sha256=", ":branch="):
        if marker in tail:
            return tail.rsplit(marker, 1)[0]
    return tail


def resolve(item: Mapping[str, Any], index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The memory record a search hit belongs to, or None if it is not one."""
    if not index:
        return None
    node_id = str(item.get("node_id") or "")
    record = index.get(node_id)
    if record is not None:
        return record
    # A node *derived* from an artifact — a chunk, symbol or entity — names the
    # artifact it came from. This is what lets the policy judge an entity label
    # extracted out of a memory record, which before Step 33.7 exposed no
    # identity at all and so survived a filter that correctly dropped the
    # record itself.
    derived_from = item.get("artifact_id")
    if derived_from:
        record = index.get(str(derived_from))
        if record is not None:
            return record
    # A relationship hit is one edge, and it belongs to memory if *either* end
    # does — `entity:…:bananas --derived_from--> <the record>` carries the
    # record's content just as surely as the record's own chunk. Its `node_id`
    # is the edge's source node, so neither of the lookups above sees the
    # target at all.
    for endpoint in ("target", "source"):
        value = item.get(endpoint)
        if value:
            record = index.get(str(value))
            if record is not None:
                return record
    provenance = item.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    source_id = item.get("source_id") or provenance.get("source_id") or ""
    relative_path = (
        item.get("relative_path") or provenance.get("relative_path") or relative_path_of(node_id)
    )
    if source_id and relative_path:
        return index.get(f"{source_id}{_PATH_KEY}{relative_path}")
    return None


def describe(record: Mapping[str, Any]) -> dict[str, Any]:
    """What a search result says about the memory it came from."""
    return {
        "record_id": record.get("record_id"),
        "scope": record.get("scope"),
        "subject": record.get("subject"),
        "kind": record.get("kind"),
        "asserted_at": record.get("asserted_at"),
        # Phase 3, additive: a hit only reaches here at all when its tier is
        # in `allowed_tiers(policy)`, so `cold` only ever appears when the
        # caller explicitly asked for history — worth labeling, not hiding.
        "tier": str(record.get("tier") or "hot"),
    }


# Deprecated import name; policies now apply to typed artifacts.
MemoryPolicy = ArtifactPolicy
