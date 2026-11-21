"""The measured capacity model (Phase 35.7).

Every sizing question — how much RAM, how much disk, how long will the first
index take, when do I shard — comes down to a handful of coefficients. This
module is the one place they live, so `pheasant scan`, `pheasant shard plan`
and the documentation cannot disagree with each other.

**These are measurements, not estimates.** Each constant records where it came
from, and `python -m pheasant.sync.benchmark --mode capacity` reproduces the
run that produced it. Re-run it on your own hardware: the *shape* holds (all
of this is linear in file count), the constants move.

A projection from a file count is necessarily coarse. It is still worth having
because the alternative is not a better number, it is no number — and the
failure it prevents is specific: pointing a source at something large,
watching a container get OOM-killed mid-sync, and reading the restart as a bug
rather than as a capacity limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pheasant.sync.saturation import SHARD_THRESHOLD

#: Graph nodes per file. From the live 2,132-file demo corpus (13,503 nodes).
#:
#: The synthetic sweep measures **3.0** and is *not* used here, which is worth
#: saying out loud: its fixture is uniform markdown with no links, no headings
#: and no cross-references, so it produces a file node plus chunk nodes and
#: nothing else. A real corpus adds symbols, entities and headings. Taking the
#: synthetic number would have halved every memory projection on the strength
#: of a fixture that was never meant to be representative.
NODES_PER_FILE = 6.3

#: Process RSS per graph node **for a process that holds the graph**, measured
#: flat across four scales in `pheasant.graph.capacity` (2k → 250k files).
#:
#: That is the indexer and nothing else: a serving replica queries
#: `graph_nodes` and its measured graph RSS is zero.
RSS_BYTES_PER_NODE = 2400

#: The graph is roughly this share of process RSS; the rest is the
#: interpreter, the search stores and whatever is being served.
GRAPH_SHARE_OF_RSS = 0.6

#: Database bytes per graph node with ``storage.graph_format: rows``, covering
#: the node row, its one edge row and ``idx_graph_edges_target``.
#:
#: **Measured 1,637 at 20k files and 1,639 at 100k** — flat, which it should be
#: — with `python -m pheasant.graph.capacity`. Roughly 55× the compressed
#: node-link file it replaces (17.9MB vs 1,033MB at 100k files), and that is
#: the honest cost of the change: the graph stops being a thing every replica
#: holds and starts being a thing the volume holds. Disk is the resource this
#: trades *for* RAM and commit latency, and a projection that hid it would
#: fill somebody's PVC.
#:
#: One node per edge is this corpus's ratio and the demo corpus's (13,503
#: nodes / 13,502 edges). A corpus with a much denser graph pays more.
GRAPH_ROW_BYTES_PER_NODE = 1640

#: State-directory bytes per *corpus* byte. **Measured 3.95–4.09** across the
#: 500→8,000-file sweep, converging on ~3.97; the value below is the round
#: number that covers it. Dominated by SQLite, which stores every chunk's text
#: a second time plus its FTS5 index — so this scales with *content*, not with
#: file count, which is why :func:`project` takes bytes and not just files.
#:
#: Vectors are excluded: a per-dimension cost that exists only when embeddings
#: are on, added separately below.
STATE_BYTES_PER_CORPUS_BYTE = 4.0

#: Vector-store bytes per chunk per dimension, at float32.
VECTOR_BYTES_PER_CHUNK_PER_DIM = 4

#: Corpus bytes per chunk. Measured at a dead-flat **2,728** across every
#: point in the sweep — but that is this fixture's file size (5.4 KB → two
#: chunks) as much as it is the chunker. It rises toward
#: ``chunking.max_chars`` for a corpus of larger documents, so treat it as a
#: floor. Bytes-per-chunk rather than chunks-per-file because the former is
#: the stable one: file size varies far more between corpora than chunk size
#: does.
BYTES_PER_CHUNK = 2728

#: Seconds per 1,000 files for a full index, one worker, embeddings off, on
#: the machine that ran the sweep. **Measured 2.81–3.28** after the O(N²) fix
#: below; the high end is used because it is the large-corpus figure.
#:
#: Before that fix this was not a constant at all — it went 3.85 → 20.58 over
#: 500 → 8,000 files, because ``chunks_fts.artifact_id`` is UNINDEXED and the
#: per-artifact DELETE scanned the whole growing table. Anyone quoting a
#: seconds-per-file figure from a small corpus was extrapolating a curve as
#: though it were a line.
SECONDS_PER_1K_FILES = 3.3

#: Multiplier on the above when embeddings are enabled with a real provider.
#: **Deliberately not measured**: a network embedder's throughput is the
#: provider's rate limit, not pheasant's, and a number derived from the stub
#: would describe nothing. Recorded as a documented unknown rather than a
#: fabricated constant — see `docs/how-to/capacity-planning.md`.
EMBEDDING_TIME_MULTIPLIER: float | None = None

#: Hours of single-indexer indexing past which a corpus is worth planning
#: differently. Derived, not a new guess: it is ``SECONDS_PER_1K_FILES``
#: applied to the file count `pheasant scan` already has.
#:
#: The number exists because the ceiling it describes was prose. One indexer
#: is the sole commit authority for a knowledge base — the graph, the vectors
#: and the graph FTS are one coordinated commit stream, and extra indexers for
#: one shard are elected hot standbys rather than throughput. So an initial
#: index is not a job that goes faster with more replicas, and a team that
#: discovers that after provisioning them has learned it the expensive way.
#:
#: Twelve hours because that is roughly where a first index stops fitting in
#: an overnight window, which is the practical unit an operator plans in.
#: Below it the ceiling is real and irrelevant; above it, it is the thing that
#: decides the shape of the deployment.
COMMIT_AUTHORITY_WARN_HOURS = 12.0

#: Sustained commit-authority saturation past which sharding is the answer and
#: more workers are not. Imported rather than restated so a capacity report and
#: a live gauge cannot disagree about where the line is — the same reason
#: `sharding.py` re-exports the three sizing constants from here.
SHARD_ON_SATURATION = SHARD_THRESHOLD

#: Container sizes an operator would actually type.
_MEMORY_LADDER = (0.5, 1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64)


@dataclass(frozen=True)
class Projection:
    """What one corpus is expected to cost."""

    files: int
    corpus_bytes: int
    nodes: int
    chunks: int
    rss_bytes: int
    state_bytes: int
    index_seconds: float
    recommended_memory: str
    shards: int
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "corpus_bytes": self.corpus_bytes,
            "corpus_mb": round(self.corpus_bytes / (1024 * 1024), 1),
            "graph_nodes": self.nodes,
            "chunks": self.chunks,
            "projected_rss_bytes": self.rss_bytes,
            "projected_rss_gb": round(self.rss_bytes / 1e9, 2),
            "projected_state_bytes": self.state_bytes,
            "projected_state_gb": round(self.state_bytes / 1e9, 2),
            "projected_index_seconds": round(self.index_seconds, 1),
            "projected_index_minutes": round(self.index_seconds / 60, 1),
            "recommended_memory": self.recommended_memory,
            "recommended_shards": self.shards,
            "warnings": list(self.warnings),
        }


def recommend_memory(rss_bytes: int) -> str:
    """A container limit with headroom, rounded to something a human types.

    1.5x the projection, because the estimate comes from a file count and a
    corpus of unusually large documents will exceed it — and being 50% over on
    a request is cheap where being 10% under is an OOM kill mid-sync.
    """

    gib = rss_bytes * 1.5 / (1024**3)
    for candidate in _MEMORY_LADDER:
        if gib <= candidate:
            return f"{candidate:g}Gi"
    return f"{int(gib) + 1}Gi"


def project(
    files: int,
    corpus_bytes: int = 0,
    *,
    embedding_dimensions: int = 0,
    max_nodes_per_shard: int = 1_500_000,
    seconds_per_1k_files: float = SECONDS_PER_1K_FILES,
    graph_format: str = "rows",
    nodes: int | None = None,
) -> Projection:
    """Project cost from a file count and (optionally) a byte count.

    ``corpus_bytes`` is what `pheasant scan` already reports, and it is what
    makes the disk number meaningful: state size tracks *content*, while RSS
    and node count track file count. Passing 0 skips the disk projection
    rather than inventing one.

    ``graph_format`` is ``rows``: the graph is in the database and out of
    every serving process's memory, so its bytes count toward state.

    ``nodes`` overrides the ``NODES_PER_FILE`` estimate with a real count. For
    a corpus nobody has indexed yet there is nothing better than the estimate;
    for one that *has* been indexed — and for the benchmark checking this
    model against a real run — the count is known, and using the estimate
    there would compare the model against itself.
    """

    files = max(0, int(files))
    corpus_bytes = max(0, int(corpus_bytes))
    nodes = int(files * NODES_PER_FILE) if nodes is None else max(0, int(nodes))
    # From bytes where we have them: a corpus of 100 large files and one of
    # 100 small ones differ by an order of magnitude in chunks and not at all
    # in file count. Falling back to one chunk per file is the floor, since
    # even an empty document produces one.
    chunks = int(corpus_bytes / BYTES_PER_CHUNK) if corpus_bytes else files
    chunks = max(chunks, files)
    rss = int(nodes * RSS_BYTES_PER_NODE / GRAPH_SHARE_OF_RSS)

    state = int(corpus_bytes * STATE_BYTES_PER_CORPUS_BYTE) if corpus_bytes else 0
    if embedding_dimensions > 0:
        state += chunks * embedding_dimensions * VECTOR_BYTES_PER_CHUNK_PER_DIM
    if graph_format == "rows":
        state += nodes * GRAPH_ROW_BYTES_PER_NODE

    seconds = files / 1000.0 * seconds_per_1k_files
    shards = max(1, -(-nodes // max(1, max_nodes_per_shard)))  # ceil

    warnings: list[str] = []
    if shards > 1:
        warnings.append(
            f"{nodes:,} nodes exceeds the {max_nodes_per_shard:,}-node budget for one "
            f"region; `pheasant shard plan` proposes a {shards}-way split."
        )
    if seconds > COMMIT_AUTHORITY_WARN_HOURS * 3600:
        warnings.append(
            f"a first index of {files:,} files takes about {seconds / 3600:.0f}h on one "
            "indexer, and one indexer is the whole of a region's commit capacity — extra "
            "indexers for a shard are hot standbys, not throughput. Split the corpus "
            "(`pheasant shard plan`) or plan the window; watch "
            f"pheasant_commit_authority_saturation (shard above {SHARD_ON_SATURATION}) "
            "once it is running."
        )
    if embedding_dimensions > 0 and EMBEDDING_TIME_MULTIPLIER is None:
        warnings.append(
            "index time excludes embedding: a network embedder's throughput is the "
            "provider's rate limit, not pheasant's, and is not projected here."
        )
    return Projection(
        files=files,
        corpus_bytes=corpus_bytes,
        nodes=nodes,
        chunks=chunks,
        rss_bytes=rss,
        state_bytes=state,
        index_seconds=seconds,
        recommended_memory=recommend_memory(rss),
        shards=shards,
        warnings=tuple(warnings),
    )


#: Bytes one interaction row costs in ``/state``, including its indexes.
#:
#: Measured on SQLite with a WAL checkpoint, over a realistic row: a query, a
#: criteria object, five result ids and five paths, and the usual attributes.
#: 892 B at 5k rows, 937 at 20k, 949 at 50k --- flat across a 10x range, so the
#: number is the row and not the page overhead of a small file. Rounded up.
BYTES_PER_INTERACTION_EVENT = 950

#: The same row when a chat answer rides along, at the 4,000-character default
#: cap. 2,124 B at 5k rows and 2,169 at 20k.
#:
#: The ratio is the point: **a chat turn costs about 2.3x a search**, which is
#: why `max_answer_chars` exists and why `0` turns answers off entirely. A
#: region whose traffic is mostly chat should size for this row, not the other
#: one.
BYTES_PER_CHAT_INTERACTION = 2200


#: ``/state`` bytes per stored proof row. A proof is short and fixed-shape --
#: two ids, an event name, a weight, a partition token and a multiplier bag --
#: so this is dominated by SQLite's per-row overhead and its three indexes
#: rather than by the payload. Rounded up from the measured row size on the
#: parity corpus, because being 30% over on a row this small costs nothing and
#: being under is what makes a volume fill.
BYTES_PER_EVALUATION_PROOF = 420

#: ``/state`` bytes per stored metric row. Much larger than a proof row,
#: because the payload is the whole explanation contract: the formula, the
#: substituted calculation, the operands, the proof references and the
#: limitation. That is the *point* -- a metric without them is not publishable
#: -- so the cost is a design decision rather than an overhead to trim.
#:
#: Per-query rows dominate the count; an aggregate is roughly the same size.
BYTES_PER_EVALUATION_METRIC = 2200

#: ``/state`` bytes per run's stored report. The thirteen required sections,
#: including all three explanation projections. Grows with the *cohort* count
#: rather than with the corpus, so it is flat for a region of any size.
BYTES_PER_EVALUATION_REPORT = 120_000

#: ``/state`` bytes per checkpointed (cohort, variant) replay, per query in
#: that cohort. Transient: the checkpoints exist so a restarted batch does not
#: replay from zero, and they are deleted when the run completes. They are
#: sized here anyway because the peak matters -- a volume has to hold them
#: while the run is in flight.
#:
#: **Measured 761** by ``python -m pheasant.evaluation.benchmark`` (432 replays
#: peaking at 328,956 bytes of `results_json`); rounded up. The first value
#: here was 260, a guess that understated the peak by 3x -- which is the
#: dangerous direction, because it is the number that decides whether a volume
#: fills mid-run.
BYTES_PER_REPLAY_CHECKPOINT_QUERY = 800

#: Seconds per (query, variant) replay, single process, embeddings off. One
#: replay is one `search_context` through the real hybrid path plus the metric
#: bookkeeping.
#:
#: **Measured 0.0056** by ``python -m pheasant.evaluation.benchmark`` on a
#: 30-file synthetic corpus; the value below is rounded up to cover a larger
#: index, where the lexical arm's own cost grows. Treat it as a floor: a
#: corpus with a real vector arm pays an embedding call per replay on top, and
#: that cost is the provider's rather than pheasant's.
#:
#: **The dominant term in a run's cost, and the only one worth tuning.** A
#: batch is `queries x variants x cohorts` of these, which is why
#: `maximum_queries_per_run` exists and why the honest way to scale evaluation
#: is fewer queries or a longer interval rather than more workers -- see
#: `docs/knowledge-effectiveness.md` on why replay is not distributed.
SECONDS_PER_QUERY_VARIANT = 0.008

#: Multiplier when the vector arm is enabled with a real embedding provider.
#: **Deliberately not measured**, for the same reason
#: :data:`EMBEDDING_SLOWDOWN` is not: a network embedder's throughput is the
#: provider's rate limit, not pheasant's, and every replayed query pays one
#: embedding call. Recorded as a documented unknown.
EVALUATION_EMBEDDING_SLOWDOWN: float | None = None


@dataclass(frozen=True)
class EvaluationProjection:
    """What one evaluation batch costs, and what it leaves behind.

    Separated from :class:`Projection` because it scales on a different axis:
    a corpus projection grows with *files*, and this grows with *cohort size
    times the ablation matrix*. A region with a million files and forty
    recorded queries has a large index and a trivial evaluation; the reverse
    is equally possible, and one number covering both would describe neither.
    """

    queries: int
    variants: int
    cohorts: int
    replays: int
    run_seconds: float
    peak_checkpoint_bytes: int
    state_bytes_per_run: int
    state_bytes_per_year: int
    recommended_memory: str
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "queries_per_cohort": self.queries,
            "variants": self.variants,
            "cohorts": self.cohorts,
            "replays_per_run": self.replays,
            "projected_run_seconds": round(self.run_seconds, 1),
            "projected_run_minutes": round(self.run_seconds / 60, 1),
            "peak_checkpoint_bytes": self.peak_checkpoint_bytes,
            "peak_checkpoint_mb": round(self.peak_checkpoint_bytes / (1024 * 1024), 1),
            "state_bytes_per_run": self.state_bytes_per_run,
            "state_mb_per_run": round(self.state_bytes_per_run / (1024 * 1024), 1),
            "state_bytes_per_year": self.state_bytes_per_year,
            "state_gb_per_year": round(self.state_bytes_per_year / 1e9, 2),
            "recommended_memory": self.recommended_memory,
            "warnings": list(self.warnings),
        }


def project_evaluation(
    queries_per_cohort: int,
    *,
    variants: int = 6,
    cohorts: int = 6,
    runs_per_day: float = 1.0,
    proofs_per_query: float = 2.0,
    embeddings_enabled: bool = False,
    per_query_metric_rows: int = 3,
    max_stored_per_query_results: int = 200,
) -> EvaluationProjection:
    """Size an evaluation batch: how long it runs and what it stores.

    The defaults describe the shipped configuration -- the six-variant matrix
    over six cohorts -- so a caller who knows only their cohort size gets a
    useful answer. ``runs_per_day`` is what turns a per-run cost into a volume
    requirement, and it is the number most operators get wrong: evaluation is
    triggered by *material change*, so a region indexing hourly can run one
    per hour if it is left on that trigger.

    Storage is split deliberately. ``peak_checkpoint_bytes`` is transient and
    is what a volume must have *free* during a run; ``state_bytes_per_year``
    is what accumulates. Reporting one number for both would either overstate
    the steady state or understate the peak.

    The storage figures are an **upper bound**, and knowingly so: per-query
    metric rows exist only for queries that carry positive proof, and this
    assumes every query does. A region where a quarter of queries are
    evidenced -- which is the ordinary case -- will use roughly a quarter of
    the projected steady state. Over-provisioning a volume is cheap; running
    out of one mid-run is not, so the bound points that way on purpose.

    ``per_query_metric_rows`` is 3 because three metrics emit per-query rows:
    known-positive recall at 5 and at 10, and the reciprocal rank.
    """

    queries = max(0, int(queries_per_cohort))
    variants = max(1, int(variants))
    cohorts = max(1, int(cohorts))
    replays = queries * variants * cohorts

    run_seconds = replays * SECONDS_PER_QUERY_VARIANT
    warnings: list[str] = []
    if embeddings_enabled:
        warnings.append(
            "Embeddings are on: every replayed query makes an embedding call, so the run "
            "time above is a floor bounded by your provider's rate limit, not by pheasant. "
            "No multiplier is applied because a measured one would describe the stub."
        )

    # Metric rows: per-query rows for the demonstrated metrics on every
    # (cohort, variant), plus a much smaller number of aggregates.
    #
    # Capped by `evaluation.maximum_stored_per_query_results`, because the
    # runner is: a model that ignored its own configuration knob would
    # overstate a large cohort by the ratio between its size and that cap,
    # which is exactly the case where the number matters.
    stored_per_metric = min(queries, max(0, int(max_stored_per_query_results)))
    metric_rows = stored_per_metric * per_query_metric_rows * variants * cohorts
    metric_rows += variants * cohorts * 24
    state_per_run = (
        metric_rows * BYTES_PER_EVALUATION_METRIC
        + BYTES_PER_EVALUATION_REPORT
        + queries * cohorts * 400  # the frozen cohort membership
    )
    # Proof is written by *traffic*, not by runs, so it is counted once per
    # year rather than once per run -- the same distinction the observation
    # plane makes, and the reason the two are sized apart.
    proof_bytes = int(queries * proofs_per_query * BYTES_PER_EVALUATION_PROOF)
    per_year = int(state_per_run * max(0.0, runs_per_day) * 365) + proof_bytes

    # Every pair holds one result list per query in its cohort, and they all
    # coexist until the run completes and clears them. This is the free space a
    # volume must have *during* a run, which is a different question from what
    # it accumulates.
    peak_checkpoints = replays * BYTES_PER_REPLAY_CHECKPOINT_QUERY

    if per_year > 20 * 1024**3:
        warnings.append(
            "Over 20 GiB of evaluation state a year at this rate. Lower "
            "`evaluation.maximum_stored_per_query_results`, run less often, or prune old "
            "runs -- the per-query rows are the audit trail, and they are most of this."
        )
    if run_seconds > 900:
        warnings.append(
            "A run projects past the default `maximum_runtime_seconds` (900s) and would be "
            "truncated. Lower `maximum_queries_per_run`, or raise the budget deliberately."
        )

    # Replay holds one cohort's result lists at a time, plus the report. Small
    # next to the graph, which is why an evaluation-capable process is sized by
    # the corpus rather than by this -- but not zero, and worth stating.
    rss = 256 * 1024**2 + replays * 4096
    return EvaluationProjection(
        queries=queries,
        variants=variants,
        cohorts=cohorts,
        replays=replays,
        run_seconds=run_seconds,
        peak_checkpoint_bytes=peak_checkpoints,
        state_bytes_per_run=state_per_run,
        state_bytes_per_year=per_year,
        recommended_memory=recommend_memory(rss),
        warnings=tuple(warnings),
    )


def observation_state_bytes(
    events_per_day: int,
    retention_days: int,
    *,
    chat_share: float = 0.0,
) -> int:
    """How much ``/state`` an enabled observation plane will hold.

    Retention-bounded rather than corpus-bounded, which makes it the one part
    of ``/state`` that grows with *traffic* --- the reason the log tier scales
    on its own axis. ``chat_share`` is the fraction of observations that carry
    an answer.
    """

    share = min(1.0, max(0.0, float(chat_share)))
    per_event = BYTES_PER_INTERACTION_EVENT * (1.0 - share) + BYTES_PER_CHAT_INTERACTION * share
    return int(max(0, events_per_day) * max(0, retention_days) * per_event)


def fleet_for(files: int) -> dict[str, Any]:
    """Replica counts for a corpus of this size.

    Deliberately coarse, and derived rather than tabulated: api replicas track
    *request* traffic, which this model knows nothing about, so the number
    here is a floor for availability (two, so a rollout is not an outage) that
    grows only where the graph is large enough that one replica's memory is
    the constraint. Workers track indexing throughput, which is what the file
    count actually predicts.
    """

    projection = project(files)
    shards = projection.shards
    # One worker per ~25k files, bounded: past a point the coordinator's
    # commit path is the bottleneck, not preparation, and the 2026-08-13
    # benchmark measured 8 file workers buying 1.113x.
    workers = max(0, min(8, files // 25_000)) * shards
    return {
        "shards": shards,
        "indexers": shards,
        "api_replicas": 2 if files < 250_000 else 3,
        "workers": workers,
        # The log tier scales on request traffic, which a file count cannot
        # predict at all -- so this is a floor for a region that has enabled
        # observation, not a projection. One replica handles a great deal: a
        # batch is 500 observations and the work per batch is a multi-row
        # insert. Scale it on `pheasant_log_queue_depth`, never on this.
        "loggers": 1,
        "memory_per_region": projection.recommended_memory,
        "single_container_is_enough": shards == 1 and files < 150_000,
    }
