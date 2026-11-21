"""Planning a shard split (Phase 35.4).

The capacity work measured where one region stops being sensible; this turns
that into a concrete proposal — *these sources go to region 1, those to region
2* — with the configs to run them.

**Shards are whole sources.** That is the load-bearing decision, and it is not
about implementation convenience. Retrieval quality depends on related content
living in one graph: `resolve_cross_source_edges` links an import or a Markdown
link to the file it names, and `get_graph_neighbors` walks those links. Splitting
one repository across two regions severs exactly the edges that make the graph
worth having, while splitting *between* repositories costs nothing, because
those edges were never going to exist.

Hashing paths across shards — the obvious mechanical answer, and the one the
plan originally called for — would do the opposite: it distributes evenly and
severs every edge. Even balance is the wrong objective when the thing being
balanced is a graph.

So this is bin-packing over sources, greedy largest-first, which is the classic
LPT heuristic and lands within 4/3 of optimal. Optimal packing is NP-hard and
the input is a size estimate, so a guaranteed-close answer from a
one-line-explainable rule beats an exact answer to the wrong question.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pheasant.capacity import GRAPH_SHARE_OF_RSS, NODES_PER_FILE, RSS_BYTES_PER_NODE

# Re-exported, not redefined. These were a second copy of the same three
# numbers, so `pheasant shard plan` and `pheasant scan` could be edited apart
# and quietly disagree about how big the same corpus is — which is the whole
# reason :mod:`pheasant.capacity` exists (Phase 35.7).
__all__ = [
    "GRAPH_SHARE_OF_RSS",
    "NODES_PER_FILE",
    "RSS_BYTES_PER_NODE",
    "Shard",
    "SourceSize",
    "plan_shards",
    "render_artifacts",
    "render_plan",
]


@dataclass
class SourceSize:
    """What one source contributes. ``files`` is all the planner needs."""

    name: str
    files: int
    bytes_: int = 0

    @property
    def nodes(self) -> int:
        return int(self.files * NODES_PER_FILE)


@dataclass
class Shard:
    index: int
    sources: list[SourceSize] = field(default_factory=list)

    @property
    def files(self) -> int:
        return sum(source.files for source in self.sources)

    @property
    def nodes(self) -> int:
        return sum(source.nodes for source in self.sources)

    @property
    def rss_bytes(self) -> int:
        return int(self.nodes * RSS_BYTES_PER_NODE / GRAPH_SHARE_OF_RSS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "shard": self.index,
            "region_name": f"shard-{self.index}",
            "sources": [source.name for source in self.sources],
            "files": self.files,
            "nodes": self.nodes,
            "projected_rss_bytes": self.rss_bytes,
            "projected_rss_gb": round(self.rss_bytes / 1e9, 2),
            "recommended_memory": _recommend_memory(self.rss_bytes),
        }


def _recommend_memory(rss_bytes: int) -> str:
    """A container limit with headroom, rounded to something a human types.

    1.5x the projection, because the estimate is derived from a file count and
    a corpus of unusually large documents will exceed it — and being 50% over
    on a request is cheap where being 10% under is an OOM kill mid-sync.
    """

    gib = rss_bytes * 1.5 / (1024**3)
    for candidate in (0.5, 1, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64):
        if gib <= candidate:
            return f"{candidate:g}Gi"
    return f"{int(gib) + 1}Gi"


def plan_shards(
    sources: list[SourceSize],
    *,
    shards: int | None = None,
    max_nodes_per_shard: int = 1_500_000,
) -> dict[str, Any]:
    """Propose a split.

    With ``shards`` given, packs into exactly that many. Otherwise picks the
    smallest count that keeps every shard under ``max_nodes_per_shard`` —
    which defaults to ``graph.max_nodes``, so the planner and the runtime
    warning agree about what "too big" means.
    """

    usable = [source for source in sources if source.files > 0]
    if not usable:
        return {"shards": [], "warnings": ["no sources with any files to plan"]}

    total_nodes = sum(source.nodes for source in usable)
    if shards is None:
        needed = -(-total_nodes // max(1, max_nodes_per_shard))  # ceil
        shards = max(1, needed)
    shards = max(1, min(int(shards), len(usable)))

    # Largest-first (LPT). Placing big sources while every bin is still empty
    # is what keeps the biggest one from landing on an already-full shard,
    # which is the failure mode of first-fit in source order.
    ordered = sorted(usable, key=lambda source: (-source.files, source.name))
    bins = [Shard(index=position + 1) for position in range(shards)]
    for source in ordered:
        smallest = min(bins, key=lambda shard: (shard.nodes, shard.index))
        smallest.sources.append(source)

    warnings: list[str] = []
    for shard in bins:
        if shard.nodes > max_nodes_per_shard:
            # One source can exceed the budget on its own, and no arrangement
            # of whole sources fixes that. Say so rather than proposing a
            # split that cannot honour it.
            biggest = max(shard.sources, key=lambda source: source.files)
            warnings.append(
                f"shard {shard.index} holds {shard.nodes:,} nodes, over the "
                f"{max_nodes_per_shard:,} budget — '{biggest.name}' alone is "
                f"{biggest.nodes:,}. Give that region more memory, or narrow the "
                f"source with include/exclude or a depth cap."
            )
    empty = [shard.index for shard in bins if not shard.sources]
    if empty:
        warnings.append(
            f"shard(s) {empty} would be empty: there are only {len(usable)} sources "
            "to spread, so fewer shards is the honest answer."
        )

    return {
        "total_files": sum(source.files for source in usable),
        "total_nodes": total_nodes,
        "shard_count": shards,
        "max_nodes_per_shard": max_nodes_per_shard,
        "shards": [shard.as_dict() for shard in bins if shard.sources],
        "warnings": warnings,
    }


def render_plan(plan: dict[str, Any]) -> str:
    """The human-readable form printed by ``pheasant shard plan``."""

    lines: list[str] = []
    if not plan.get("shards"):
        return "Nothing to plan: no sources with any files.\n"
    lines.append(
        f"{plan['total_files']:,} files across {len(plan['shards'])} region(s) "
        f"(~{plan['total_nodes']:,} graph nodes)"
    )
    lines.append("")
    for shard in plan["shards"]:
        lines.append(
            f"  {shard['region_name']}: {shard['files']:,} files, "
            f"~{shard['nodes']:,} nodes, ~{shard['projected_rss_gb']:.1f} GB RSS "
            f"-> request {shard['recommended_memory']}"
        )
        for name in shard["sources"]:
            lines.append(f"      - {name}")
    if plan.get("warnings"):
        lines.append("")
        for warning in plan["warnings"]:
            lines.append(f"  ! {warning}")
    lines.append("")
    lines.append(
        "Each region is a separate knowledge base with its own state; a question "
        "is answered by one region, never across them. "
        "See docs/how-to/capacity-planning.md."
    )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# From a proposal to a pull request (Phase 35.8)
#
# The plan said *which sources go where* and stopped there, so acting on it
# meant hand-writing a second project: another config, another compose file,
# another set of volume names, another port, another three secrets — and
# getting every one of them different from the first, because a second region
# that reuses the first's `pheasant.name` shares its knowledge-base id, and
# every stable ID in this system starts with that.
#
# That is a weekend of careful copying with a silent failure mode at the end
# of it. Emitting the files makes it a diff somebody reviews instead.
#
# What this deliberately does NOT do: apply anything, move any data, or claim
# the split is complete. Each region indexes its own sources from scratch;
# there is no state migration here and pretending otherwise would be the
# dangerous kind of automation.
# ---------------------------------------------------------------------------

#: Host port for the first emitted region. Successive regions take the next
#: port up, so `docker compose up` in two directories does not collide.
BASE_PORT = 8765


def _region_config(config: Any, shard: dict[str, Any], *, kb_id: str) -> dict[str, Any]:
    """This region's `pheasant.yaml`: the same config, its own name and sources.

    Everything else is copied verbatim on purpose. A shard is not a different
    product — it ranks, chunks and enriches identically, and a split that
    quietly changed retrieval settings would make the two regions answer
    differently for reasons nobody could see in the plan.
    """

    data = config.model_dump(mode="json") if hasattr(config, "model_dump") else dict(config)
    data = dict(data)
    pheasant = dict(data.get("pheasant") or {})
    # The knowledge-base id, and therefore the prefix of every artifact id,
    # chunk id and graph node id this region will ever write. Two regions
    # sharing it is the one mistake that cannot be fixed by editing a file
    # later: it would already be inside the persisted graph.
    pheasant["name"] = kb_id
    pheasant["description"] = (
        f"{pheasant.get('description', 'pheasant region')} — shard {shard['shard']}"
    )
    # The compose file beside this mounts volumes at the container paths, so
    # the emitted config names those rather than inheriting wherever the
    # planning machine happened to keep its state.
    pheasant["state_path"] = "/state"
    pheasant["workspace_root"] = "/workspace"
    pheasant["exports_path"] = "/exports"
    data["pheasant"] = pheasant
    wanted = set(shard["sources"])
    data["sources"] = [
        source for source in (data.get("sources") or []) if source.get("name") in wanted
    ]
    return data


def _compose_memory(recommended: str) -> str:
    """`4Gi` -> `4096m`.

    Docker's `mem_limit` takes an integer with a b/k/m/g suffix, so the
    half-gigabyte recommendation the ladder starts at (`0.5Gi`) is not a value
    it accepts — and a compose file that fails to parse is a worse deliverable
    than no compose file.
    """

    try:
        gib = float(str(recommended).rstrip("Gig").rstrip("i") or 1)
    except ValueError:
        gib = 2.0
    return f"{max(256, int(gib * 1024))}m"


def _region_compose(
    kb_id: str,
    *,
    port: int,
    memory: str,
    image: str,
    source_paths: dict[str, str] | None = None,
) -> str:
    """One container per region, which is what a shard is.

    Deliberately the single-container shape rather than the role-split fleet:
    sharding is what a region does when it is too big for one *graph*, and
    each resulting region is by construction small enough for one process
    again. A region that then outgrows that has `docker-compose.scale.yml`
    waiting, and this file is the wrong place to guess which of the two an
    operator wants.
    """

    return f"""# {kb_id} — one region of a sharded corpus. Generated by `pheasant shard plan
# --emit`; edit freely, and keep `name:` and the volume names distinct from
# every other region or two projects will share one state directory.
#
#   cp .env.example .env   # then fill in the three secrets
#   docker compose --env-file .env up -d
name: {kb_id}

services:
  pheasant:
    image: ${{PHEASANT_IMAGE:-{image}}}
    restart: unless-stopped
    init: true
    stop_grace_period: 30s
    # From the projected graph size for this shard's sources; see
    # `pheasant scan` and docs/how-to/capacity-planning.md.
    mem_limit: {_compose_memory(memory)}
    environment:
      PHEASANT_CONFIG: /config/pheasant.yaml
      # Generate a value per region and per boundary. See .env.example.
      PHEASANT_API_TOKEN: ${{PHEASANT_API_TOKEN:-}}
      OPENAI_API_KEY: ${{OPENAI_API_KEY:-}}
    ports:
      # Loopback. Widen only behind an authenticating ingress.
      - "${{PHEASANT_BIND:-127.0.0.1}}:${{PHEASANT_PORT:-{port}}}:8765"
    volumes:
      - ./pheasant.yaml:/config/pheasant.yaml:ro
      - {kb_id}-state:/state
      - {kb_id}-workspace:/workspace
      - {kb_id}-exports:/exports
{_source_mount_hints(source_paths)}
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:8765/health || exit 1"]
      interval: 60s
      timeout: 10s
      retries: 5
      start_period: 30s

volumes:
  {kb_id}-state:
  {kb_id}-workspace:
  {kb_id}-exports:
"""


def _source_paths(config: Any, shard: dict[str, Any]) -> dict[str, str]:
    """Filesystem paths this region's sources read, by source name."""

    wanted = set(shard["sources"])
    paths: dict[str, str] = {}
    for source in getattr(config, "sources", None) or []:
        name = str(getattr(source, "name", "") or "")
        path = getattr(source, "path", None)
        if name in wanted and path:
            paths[name] = str(path)
    return paths


def _source_mount_hints(source_paths: dict[str, str] | None) -> str:
    """Commented bind mounts for content that lives outside the volume.

    Commented rather than emitted live, and that is the honest line: this
    machine's paths are not necessarily the deploying machine's, and a compose
    file that silently bind-mounted a guess would either fail to start or
    index the wrong directory. Naming them is help; assuming them is not.
    """

    if not source_paths:
        return ""
    lines = [
        "      # Sources this region indexes from the host. Mount each one read-only if",
        "      # it is not already inside the workspace volume above — the paths below are",
        "      # this machine's, so check them against the deploying host.",
    ]
    for name, path in sorted(source_paths.items()):
        lines.append(f"      #   - {path}:/workspace/{name}:ro")
    return "\n".join(lines)


def _region_env_example(kb_id: str) -> str:
    return f"""# Secrets for the {kb_id} region. Copy to `.env`; never commit the copy.
#
# One `openssl rand -hex 32` per line, and per region. A value shared between
# two regions makes either one's compromise the other's, and a value shared
# between two boundaries inside a region does the same thing one level down —
# which is a mistake this repository has already shipped once and now refuses
# at startup.

# Required once this region binds anything other than loopback, and required
# by every role but `all`. Callers send it as `Authorization: Bearer <value>`.
# PHEASANT_API_TOKEN=

# Only if this region grows into the role-split fleet
# (deploy/compose/docker-compose.scale.yml). Three distinct values.
# PHEASANT_GRAPH_SERVICE_TOKEN=
# PHEASANT_INDEX_WORKER_TOKEN=
# POSTGRES_PASSWORD=

# Optional: embeddings, captioning, transcription and chat all read this by
# default. A region with none of them enabled needs no key at all.
# OPENAI_API_KEY=
"""


def _emit_readme(plan: dict[str, Any], regions: list[str]) -> str:
    lines = [
        "# A sharded corpus, as files",
        "",
        "Generated by `pheasant shard plan --emit`. Nothing here has been applied.",
        "",
        f"{plan['total_files']:,} files split across {len(regions)} regions "
        f"(~{plan['total_nodes']:,} graph nodes), packed by whole source: related",
        "content stays in one graph, because that is what `resolve_cross_source_edges`",
        "and `get_graph_neighbors` walk. Splitting *between* repositories costs nothing;",
        "splitting one *across* regions severs exactly the edges worth having.",
        "",
        "## What to do with it",
        "",
        "```bash",
    ]
    for region in regions:
        lines.append(f"cd {region} && cp .env.example .env  # fill in, then:")
        lines.append("docker compose --env-file .env up -d")
    lines.extend(
        [
            "```",
            "",
            "## What this does not do",
            "",
            "- **No data moves.** Each region indexes its own sources from scratch.",
            "  There is no state migration, and a tool that pretended otherwise would",
            "  be the dangerous kind of automation.",
            "- **Queries do not span regions.** Each region is its own knowledge base;",
            "  a question asked of one is answered from that region's sources only.",
            "- **No secrets are generated.** The `.env.example` files carry stubs and",
            "  say how many distinct values each region needs.",
            "",
            "## Regions",
            "",
        ]
    )
    for shard in plan["shards"]:
        lines.append(
            f"- **{shard['region_name']}** — {shard['files']:,} files, "
            f"~{shard['nodes']:,} nodes, request {shard['recommended_memory']}: "
            + ", ".join(shard["sources"])
        )
    if plan.get("warnings"):
        lines.extend(["", "## Warnings carried from the plan", ""])
        lines.extend(f"- {warning}" for warning in plan["warnings"])
    return "\n".join(lines) + "\n"


def render_artifacts(
    plan: dict[str, Any],
    config: Any,
    *,
    image: str = "ghcr.io/esatt10/pheasant:latest",
    base_port: int = BASE_PORT,
) -> dict[str, str]:
    """The files a shard split needs, as {relative path: content}.

    Returned rather than written so the shape is testable without a
    filesystem, and so the CLI owns the one decision this module should not
    make: whether overwriting somebody's edited region config is acceptable.
    """

    from pheasant.config.loader import dump_config_yaml

    base_id = str(getattr(getattr(config, "pheasant", None), "name", "") or "pheasant")
    artifacts: dict[str, str] = {}
    regions: list[str] = []
    for offset, shard in enumerate(plan.get("shards") or []):
        kb_id = f"{base_id}-{shard['region_name']}"
        regions.append(kb_id)
        artifacts[f"{kb_id}/pheasant.yaml"] = dump_config_yaml(
            _region_config(config, shard, kb_id=kb_id)
        )
        artifacts[f"{kb_id}/docker-compose.yml"] = _region_compose(
            kb_id,
            port=base_port + offset,
            memory=str(shard["recommended_memory"]),
            image=image,
            source_paths=_source_paths(config, shard),
        )
        artifacts[f"{kb_id}/.env.example"] = _region_env_example(kb_id)
    if regions:
        artifacts["README.md"] = _emit_readme(plan, regions)
    return artifacts
