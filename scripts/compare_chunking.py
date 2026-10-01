"""Compare chunking strategies on real corpora: cost, and what retrieval gets.

`chunking.strategy: auto` is opt-in until a comparison like this says it
should not be. This builds one throwaway region per strategy over the same
corpora, indexes them, and reports two things side by side.

**Cost** -- the latency the planner was supposed to keep in check: index time,
chunk count, and characters indexed (what the embedder is billed for, overlap
included).

**Retrieval** -- two query sets, and they are not equally trustworthy:

* ``--judgements``: expert-judged queries (`scripts/fetch_benchmark_corpus.py`
  writes SciFact's). Document-level MRR@10 and recall@5/@10. This is the
  number to believe; nobody here wrote the answers.
* ``--section-queries N``: per corpus, N headings drawn with a fixed seed; the
  query is the heading's *title* and a hit is a result chunk whose text
  contains that heading's line. It measures "can I find section X", which is
  what structured documents are for -- and it favours any strategy that puts
  headings into chunk labels, so read it as a statement about findability of
  sections, not about retrieval in general.

Offline once a corpus is on disk; the stub embedder is a bag-of-words hasher,
so the ``hybrid`` mode here is lexical twice over and says nothing semantic.

    python scripts/fetch_benchmark_corpus.py --out /tmp/bench/scifact
    python scripts/compare_chunking.py \\
        --corpus scifact=/tmp/bench/scifact --judgements /tmp/bench/benchmark.json \\
        --corpus docs=docs --section-queries 60
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import time
from pathlib import Path
from typing import Any

from pheasant.config.schema import PheasantConfig
from pheasant.ingestion.taxonomy import detect_headings
from pheasant.mcp_server.tools import PheasantTools

STRATEGIES: dict[str, dict[str, Any]] = {
    "fixed": {"chunking": {"strategy": "fixed"}},
    "fixed+taxonomy": {"chunking": {"strategy": "fixed"}, "taxonomy": {"enabled": True}},
    "auto": {"chunking": {"strategy": "auto"}},
}
INCLUDE = ["**/*.md", "**/*.pdf", "**/*.txt", "**/*.docx"]


def _region(state: Path, corpora: dict[str, Path], strategy: str, args: Any) -> PheasantTools:
    sources = []
    for name, path in corpora.items():
        block = json.loads(json.dumps(STRATEGIES[strategy]))
        block.setdefault("chunking", {}).update(
            max_chars=args.max_chars, overlap_chars=args.overlap_chars
        )
        sources.append(
            {"name": name, "type": "document_folder", "path": str(path), "include": INCLUDE, **block}
        )
    config = PheasantConfig.model_validate(
        {
            "pheasant": {
                "name": f"compare-{strategy}",
                "state_path": str(state / "state"),
                "workspace_root": str(state),
                "exports_path": str(state / "exports"),
            },
            "storage": {"graph_snapshots": False},
            "security": {"allow_workspace_roots": [str(p.resolve()) for p in corpora.values()]},
            "search": {"embeddings": {"enabled": True, "provider": "stub"}},
            "sources": sources,
        }
    )
    return PheasantTools(config)


def _section_queries(corpora: dict[str, Path], per_corpus: int) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for name, root in corpora.items():
        found: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*.md")) + sorted(root.rglob("*.txt")):
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            for heading in detect_headings(text, rules=("markdown", "keyword", "numbered")):
                title = heading.title.strip()
                if len(title.split()) >= 2:
                    found.append(
                        {
                            "corpus": name,
                            "query": title,
                            "line": lines[heading.line - 1].strip(),
                        }
                    )
        random.Random(f"sections:{name}").shuffle(found)
        queries.extend(found[:per_corpus])
    return queries


def _chunk_text(tools: PheasantTools, chunk_id: str) -> str:
    rows = tools.engine.state.rows("SELECT text FROM chunks WHERE id=?", (chunk_id,))
    return rows[0]["text"] if rows else ""


def _measure(tools: PheasantTools, kb: str, args: Any, judged: Any, sections: Any) -> dict:
    out: dict[str, Any] = {}
    for mode in args.modes:
        if judged:
            reciprocal, recall5, recall10 = [], [], []
            for query in judged["queries"]:
                positives = {e["path"] for e in judged["evidence"] if e["query"] == query["query"]}
                if not positives:
                    continue
                results = tools.search_context(kb, query["query"], mode=mode, max_results=30)
                ranked: list[str] = []
                for result in results["results"]:
                    path = Path(str(result.get("relative_path") or "")).name
                    if path and path not in ranked:
                        ranked.append(path)
                hits = [rank for rank, path in enumerate(ranked[:10], 1) if path in positives]
                reciprocal.append(1 / hits[0] if hits else 0.0)
                recall5.append(len(positives & set(ranked[:5])) / len(positives))
                recall10.append(len(positives & set(ranked[:10])) / len(positives))
            out[f"{mode}.judged.mrr@10"] = round(sum(reciprocal) / len(reciprocal), 4)
            out[f"{mode}.judged.recall@5"] = round(sum(recall5) / len(recall5), 4)
            out[f"{mode}.judged.recall@10"] = round(sum(recall10) / len(recall10), 4)
            out[f"{mode}.judged.n"] = len(reciprocal)
        if sections:
            hit1 = hit5 = 0
            for query in sections:
                results = tools.search_context(
                    kb, query["query"], mode=mode, max_results=5, source_name=query["corpus"]
                )
                texts = [
                    _chunk_text(tools, str(r.get("chunk_id")))
                    for r in results["results"]
                    if r.get("chunk_id")
                ]
                found = [position for position, text in enumerate(texts) if query["line"] in text]
                hit1 += bool(found and found[0] == 0)
                hit5 += bool(found)
            out[f"{mode}.sections.hit@1"] = round(hit1 / len(sections), 4)
            out[f"{mode}.sections.hit@5"] = round(hit5 / len(sections), 4)
            out[f"{mode}.sections.n"] = len(sections)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", action="append", required=True, help="name=path")
    parser.add_argument("--judgements", type=Path)
    parser.add_argument("--section-queries", type=int, default=0)
    parser.add_argument("--strategies", default=",".join(STRATEGIES))
    parser.add_argument("--modes", default="text,hybrid")
    parser.add_argument("--max-chars", type=int, default=4000)
    parser.add_argument("--overlap-chars", type=int, default=400)
    args = parser.parse_args(argv)
    args.modes = args.modes.split(",")
    corpora = {pair.split("=", 1)[0]: Path(pair.split("=", 1)[1]) for pair in args.corpus}
    judged = json.loads(args.judgements.read_text()) if args.judgements else None
    sections = _section_queries(corpora, args.section_queries) if args.section_queries else []

    report: dict[str, Any] = {}
    for strategy in args.strategies.split(","):
        with tempfile.TemporaryDirectory(prefix=f"chunking-{strategy}-") as scratch:
            tools = _region(Path(scratch), corpora, strategy, args)
            started = time.perf_counter()
            for name in corpora:
                tools.engine.sync_source(name, "full")
            elapsed = time.perf_counter() - started
            rows = tools.engine.state.rows(
                "SELECT COUNT(*) AS n, SUM(LENGTH(text)) AS chars FROM chunks", ()
            )[0]
            headings = sum(
                1
                for _node, attrs in tools.engine.graph_builder.graph.iter_nodes()
                if attrs.get("type") == "heading"
            )
            kb = tools.engine.config.pheasant.name
            report[strategy] = {
                "index_seconds": round(elapsed, 2),
                "chunks": rows["n"],
                "indexed_chars": rows["chars"],
                "headings": headings,
                **_measure(tools, kb, args, judged, sections),
            }
            tools.engine.close()
        print(json.dumps({strategy: report[strategy]}), flush=True)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
