#!/usr/bin/env python3
"""Set the Decisions gates' thresholds from labelled examples.

The Decisions API returns probabilities; ``assistant.decisions`` acts on them
through thresholds, and the API's own guidance is the rule this script
enforces: set a threshold from labelled examples and the cost of each kind of
mistake, never from the model agreeing with itself.

It asks **the questions the workflow asks**, built by the same functions
(``decision_gates.evidence_input``, ``SUFFICIENCY``/``CRITERIA``,
``_relevance_questions``, ``GROUNDED``/``RESPONSIVE``), so a threshold
calibrated here is a threshold for the prompts that run.

Two sources of labels:

``--scifact DIR``
    SciFact's expert annotations (the benchmark ``fetch_benchmark_corpus.py``
    already uses), turned into labels for all three gates:

    * **relevance** — a claim's annotated documents are relevant; the
      unannotated abstracts that share the most words with it are the
      negatives. Hard negatives on purpose — they are what retrieval returns
      — and approximate: SciFact is sparsely judged, so an unannotated
      document is *probably*, not certainly, irrelevant.
    * **sufficiency** — "is it true that <claim>?" over a window holding the
      annotated document (sufficient: an expert says it settles the claim)
      versus a window of decoys alone (insufficient).
    * **acceptance** — an answer quoting the expert's rationale sentences and
      citing their document (accepted); the same sentences cited to a decoy
      (rejected: miscitation); and an answer asserting the claim where the
      expert's label is CONTRADICT, or denying it where it is SUPPORT
      (rejected: the cited passage says the opposite).

``--labels FILE``
    Your own JSONL, one example per line::

        {"gate": "sufficiency", "question": "...", "intent": "knowledge",
         "passages": [{"title": "...", "text": "..."}], "label": true}
        {"gate": "relevance", "question": "...", "passages": [...],
         "labels": [true, false, ...]}
        {"gate": "accept", "question": "...", "answer": "... [1]",
         "passages": [{"title": "...", "text": "..."}], "label": false}

Costs say which mistake is worse and default to the workflow's: pruning a
passage the answer needed costs five times reading one it did not; calling
thin evidence sufficient, or accepting a bad answer, costs twice the extra
round or rewrite the opposite mistake buys.

Network: this calls the Decisions API (``OPENAI_API_KEY``), and ``--scifact``
downloads SciFact once into ``--cache``. Run it where the key lives — for the
lab, inside the api container.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[1]
WORD = re.compile(r"[a-z0-9]{4,}")


def _passage(index: int, title: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(title=title, snippet=text, node_id=f"label:{index}", relative_path=title)


# ------------------------------------------------------------------ SciFact


def _load_fetcher():
    spec = importlib.util.spec_from_file_location(
        "fetch_benchmark_corpus", REPO / "scripts" / "fetch_benchmark_corpus.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def scifact_examples(cache: Path, claims: int, seed: int) -> list[dict[str, Any]]:
    fetcher = _load_fetcher()
    manifest = json.loads((REPO / "benchmarks" / "scifact-retrieval.json").read_text("utf-8"))
    data_dir = fetcher.download(manifest["source"]["url"], cache)
    corpus = {
        int(row["doc_id"]): row
        for row in (json.loads(line) for line in (data_dir / "corpus.jsonl").open())
    }
    rows = [json.loads(line) for line in (data_dir / "claims_dev.jsonl").open()]
    evidenced = sorted((c for c in rows if c.get("evidence")), key=lambda c: int(c["id"]))
    chosen = evidenced[:claims]
    rng = random.Random(seed)
    words = {doc_id: set(WORD.findall(_abstract(row).lower())) for doc_id, row in corpus.items()}
    out: list[dict[str, Any]] = []
    for claim in chosen:
        cited = [int(doc_id) for doc_id in claim["evidence"] if int(doc_id) in corpus]
        if not cited:
            continue
        claim_words = set(WORD.findall(claim["claim"].lower()))
        decoys = sorted(
            (d for d in corpus if d not in cited),
            key=lambda d: (-len(claim_words & words[d]), d),
        )[:4]
        question = f"Is it true that {claim['claim'].rstrip('.')}?"
        # A retrieved passage is the chunk that matched, so an annotated
        # document is shown from its first rationale sentence — the way the
        # workflow's preview of a real hit would read.
        anchors = {
            d: min((min(r.get("sentences") or [0]) for r in claim["evidence"][str(d)]), default=0)
            for d in cited
        }
        window = [(d, True) for d in cited[:2]] + [(d, False) for d in decoys]
        rng.shuffle(window)
        out.append(
            {
                "gate": "relevance",
                "question": question,
                "passages": [_doc(corpus[d], anchors.get(d, 0)) for d, _ in window],
                "labels": [label for _, label in window],
            }
        )
        out.append(
            {
                "gate": "sufficiency",
                "question": question,
                "intent": "knowledge",
                "passages": [_doc(corpus[d], anchors.get(d, 0)) for d, _ in window],
                "label": True,
            }
        )
        out.append(
            {
                "gate": "sufficiency",
                "question": question,
                "intent": "knowledge",
                "passages": [_doc(corpus[d]) for d in decoys],
                "label": False,
            }
        )
        doc_id = cited[0]
        rationale = claim["evidence"][str(doc_id)][0]
        sentences = corpus[doc_id]["abstract"]
        quoted = " ".join(sentences[i] for i in rationale.get("sentences", [])[:2])
        verdict = str(rationale.get("label") or "").upper()
        evidence = [_doc(corpus[doc_id]), _doc(corpus[decoys[0]])]
        # Answered the way the question asks: yes or no, per the expert.
        answer_word = "No. " if verdict == "CONTRADICT" else "Yes. "
        grounded = f"{answer_word}The study reports: {quoted} [1]"
        out.append(
            {
                "gate": "accept",
                "question": question,
                "answer": grounded,
                "passages": evidence,
                "label": True,
                "kind": "grounded",
            }
        )
        out.append(
            {
                "gate": "accept",
                "question": question,
                "answer": f"{answer_word}The study reports: {quoted} [2]",
                "passages": evidence,
                "label": False,
                "kind": "miscited",
            }
        )
        flipped = (
            f"Yes. It is true that {claim['claim'].rstrip('.')} [1]."
            if verdict == "CONTRADICT"
            else f"No. It is not true that {claim['claim'].rstrip('.')} [1]."
        )
        out.append(
            {
                "gate": "accept",
                "question": question,
                "answer": flipped,
                "passages": evidence,
                "label": False,
                "kind": "contradicted",
            }
        )
    return out


def _abstract(row: dict[str, Any]) -> str:
    return " ".join(row.get("abstract") or [])


def _doc(row: dict[str, Any], start: int = 0) -> dict[str, str]:
    sentences = list(row.get("abstract") or [])
    return {
        "title": str(row.get("title") or row.get("doc_id")),
        "text": " ".join(sentences[start:] or sentences),
    }


# ------------------------------------------------------------------- asking


def ask(decider: Any, example: dict[str, Any]) -> list[tuple[float, bool]]:
    """``(probability, label)`` samples for one labelled example."""

    from pheasant.assistant import decisions
    from pheasant.assistant.workflows import decision_gates as gates

    passages = [
        _passage(i, str(p.get("title") or f"passage {i}"), str(p.get("text") or "")[:700])
        for i, p in enumerate(example["passages"], start=1)
    ]
    question = str(example["question"])
    if example["gate"] == "sufficiency":
        intent = str(example.get("intent") or "knowledge")
        result = decider.decide(
            gates.evidence_input(question, intent, passages, {}),
            [decisions.predicate("sufficient", gates.SUFFICIENCY + gates.CRITERIA.get(intent, ""))],
        )
        return [(result.probability("sufficient"), bool(example["label"]))]
    if example["gate"] == "relevance":
        result = decider.decide(
            gates.evidence_input(question, "knowledge", passages, {}),
            gates._relevance_questions(len(passages)),
        )
        return [
            (result.probability(f"relevant_{i}"), bool(label))
            for i, label in enumerate(example["labels"], start=1)
        ]
    if example["gate"] == "accept":
        lines = [f"QUESTION: {question}", "", "ANSWER:", str(example["answer"]), "", "PASSAGES:"]
        for i, passage in enumerate(example["passages"], start=1):
            lines += [
                f"[{i}] {passage.get('title') or ''}",
                str(passage.get("text") or "")[:4000],
                "",
            ]
        result = decider.decide(
            "\n".join(lines),
            [
                decisions.predicate("grounded", gates.GROUNDED),
                decisions.predicate("responsive", gates.RESPONSIVE),
            ],
        )
        # The gate accepts on the *lower* of the two, so calibrate that.
        both = min(result.probability("grounded"), result.probability("responsive"))
        return [(both, bool(example["label"]))]
    raise ValueError(f"unknown gate {example['gate']!r}")


GATE_SETTING = {
    "sufficiency": "sufficient_threshold",
    "relevance": "relevant_threshold",
    "accept": "accept_threshold",
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--scifact", type=Path, help="cache directory for the SciFact release")
    parser.add_argument("--claims", type=int, default=60)
    parser.add_argument("--seed", type=int, default=20261020)
    parser.add_argument("--labels", type=Path, help="your own labelled JSONL")
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--relevance-fn-cost", type=float, default=5.0)
    parser.add_argument("--sufficiency-fp-cost", type=float, default=2.0)
    parser.add_argument("--accept-fp-cost", type=float, default=2.0)
    parser.add_argument("--out", type=Path, help="write samples and results as JSON here")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO / "src"))
    from pheasant.assistant.decisions import Decider, DecisionUnavailable, choose_threshold

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        parser.error("OPENAI_API_KEY is not set")
    examples: list[dict[str, Any]] = []
    if args.scifact:
        examples += scifact_examples(args.scifact, args.claims, args.seed)
    if args.labels:
        examples += [
            json.loads(line) for line in args.labels.read_text("utf-8").splitlines() if line
        ]
    if not examples:
        parser.error("give --scifact and/or --labels")

    decider = Decider(api_key=key, model=args.model, base_url=args.base_url, timeout=30.0)
    samples: dict[str, list[tuple[float, bool]]] = {gate: [] for gate in GATE_SETTING}
    kinds: Counter[str] = Counter()
    failures = 0
    started = time.perf_counter()
    for example in examples:
        try:
            for probability, label in ask(decider, example):
                if probability is not None:
                    samples[example["gate"]].append((probability, label))
            kinds[f"{example['gate']}:{example.get('kind', 'label')}"] += 1
        except DecisionUnavailable as exc:
            failures += 1
            print(f"  unavailable: {exc}", file=sys.stderr)
    costs = {
        "relevance": {"false_negative_cost": args.relevance_fn_cost},
        "sufficiency": {"false_positive_cost": args.sufficiency_fp_cost},
        "accept": {"false_positive_cost": args.accept_fp_cost},
    }
    results = {gate: choose_threshold(samples[gate], **costs[gate]) for gate in GATE_SETTING}
    print(
        f"{len(examples)} labelled examples, {failures} unavailable, "
        f"{time.perf_counter() - started:.1f}s; {dict(kinds)}"
    )
    print()
    for gate, best in results.items():
        if best.get("threshold") is None:
            print(f"{gate:<12} no samples")
            continue
        positives = sorted(p for p, label in samples[gate] if label)
        negatives = sorted(p for p, label in samples[gate] if not label)
        print(
            f"{gate:<12} threshold={best['threshold']:.3f}  accuracy={best['accuracy']:.3f}  "
            f"FP={best['false_positives']} FN={best['false_negatives']}  "
            f"(n={best['samples']}: {best['positives']}+ / {best['negatives']}-; "
            f"median p+={_median(positives):.2f}, p-={_median(negatives):.2f})"
        )
    print("\nassistant:\n  decisions:")
    for gate, best in results.items():
        if best.get("threshold") is not None:
            print(f"    {GATE_SETTING[gate]}: {best['threshold']:.2f}")
    if args.out:
        args.out.write_text(
            json.dumps({"results": results, "samples": samples, "kinds": kinds}, indent=1),
            encoding="utf-8",
        )
    return 0


def _median(values: list[float]) -> float:
    if not values:
        return float("nan")
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


if __name__ == "__main__":
    raise SystemExit(main())
