"""Materialize a public retrieval benchmark as a pheasant source directory.

The demo corpus this replaces was written by the seeding script, which meant
its known-positives were written by the seeding script too — so every number
the evaluation and tuning planes produced over it was, at bottom, a
measurement of the fixture's own opinion. That is fine for asserting the
machinery runs and worthless for showing what the machinery *says*.

SciFact fixes that at the root. Each claim comes with the document whose
sentences a domain expert annotated as supporting or contradicting it, which
is the same judgement pheasant calls typed proof: somebody looked, and said so.
It is also the canonical small retrieval benchmark — one of the BEIR tasks,
and the dataset most open-source retrieval stacks report on — so the numbers
are comparable to something outside this repository.

Three properties this script owes its callers.

**Deterministic.** The subset is chosen by a stated rule with a fixed seed, not
picked. A demo corpus assembled by hand until the charts looked good would be
a worse lie than the synthetic one, because it would look real.

**Network only here.** The offline suite never runs this. It is invoked by the
screenshot script and by CI, both of which already have network, and its output
is an ordinary directory of documents that pheasant indexes through the normal
pipeline with no special casing.

**Mixed formats, on purpose.** A quarter of the abstracts are written as real
PDFs. The retrieval numbers then come from a corpus that genuinely exercised
the PDF extraction path, rather than from Markdown standing in for documents.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import re
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO / "benchmarks" / "scifact-retrieval.json"


def _slug(text: str, limit: int = 60) -> str:
    """A filename that carries signal.

    The basename is weighted 8x in the lexical arm, so naming every document
    `doc-4983.md` would hand the ranking a corpus with no filename signal at
    all — and the whole `title_weight` parameter would be measuring nothing.
    Real documents have meaningful names; the benchmark's should too.
    """

    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (cleaned[:limit].rstrip("-")) or "untitled"


def download(url: str, into: Path) -> Path:
    """Fetch and unpack the release tarball. Returns the data directory."""

    into.mkdir(parents=True, exist_ok=True)
    data_dir = into / "data"
    if data_dir.exists():
        print(f"  reusing {data_dir}")
        return data_dir
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310 - pinned URL
        payload = response.read()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        # `data` is the only member the release ships; extracting selectively
        # keeps a surprise member in a future release from landing on disk.
        members = [m for m in archive.getmembers() if m.name.startswith("data/")]
        archive.extractall(into, members=members)  # noqa: S202 - members filtered
    return data_dir


def select(data_dir: Path, manifest: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """The deterministic subset: evidenced claims, their documents, and decoys."""

    sample = manifest["sample"]
    corpus = {
        int(row["doc_id"]): row
        for row in (json.loads(line) for line in (data_dir / "corpus.jsonl").open())
    }
    claims = [json.loads(line) for line in (data_dir / "claims_dev.jsonl").open()]
    # Only claims an expert actually annotated. A claim with `cited_doc_ids`
    # but no `evidence` was cited without a rationale being recorded, which is
    # a weaker thing and not what this benchmark is here to demonstrate.
    evidenced = [claim for claim in claims if claim.get("evidence")]
    evidenced.sort(key=lambda claim: int(claim["id"]))
    chosen = evidenced[: int(sample["claims"])]

    cited: set[int] = set()
    for claim in chosen:
        cited.update(int(doc_id) for doc_id in claim["evidence"])

    rest = sorted(set(corpus) - cited)
    rng = random.Random(int(sample["seed"]))
    decoys = rng.sample(rest, min(int(sample["decoy_documents"]), len(rest)))

    documents = [corpus[doc_id] for doc_id in sorted(cited | set(decoys)) if doc_id in corpus]
    return documents, chosen


def _abstract(document: dict[str, Any]) -> str:
    return " ".join(document.get("abstract") or [])


def write_markdown(document: dict[str, Any], path: Path) -> None:
    body = "\n\n".join(document.get("abstract") or [])
    path.write_text(
        f"# {document['title']}\n\n{body}\n\n---\n\nSciFact document {document['doc_id']}.\n",
        encoding="utf-8",
    )


def write_pdf(document: dict[str, Any], path: Path) -> bool:
    """Write a real PDF. Returns False when the PDF writer is unavailable.

    Degrading to Markdown rather than failing: the benchmark's value is its
    judgements, and a missing optional writer should cost the demo its PDF
    coverage, not its corpus. The writer is pheasant's own
    (`pheasant.ingestion.pdf_write`, over PDFium), so building the corpus
    installs nothing pheasant does not already ship.
    """

    try:
        from pheasant.ingestion.pdf_write import wrapped_text_pdf
    except ImportError:  # pragma: no cover - pypdfium2 is a core dependency
        return False

    text = f"{document['title']}\n\n{_abstract(document)}"
    # Wrapped to the page and continued onto as many pages as it needs, so a
    # long abstract never silently loses its tail — which would quietly change
    # what the corpus contains and therefore what the benchmark measures.
    path.write_bytes(wrapped_text_pdf(text, font_size=9))
    return True


def materialize(documents: list[dict], out: Path, pdf_share: float) -> dict[str, Any]:
    """Write the corpus to disk as a mixed document collection."""

    out.mkdir(parents=True, exist_ok=True)
    paths: dict[int, str] = {}
    pdfs = 0
    for index, document in enumerate(documents):
        doc_id = int(document["doc_id"])
        stem = f"{_slug(document['title'])}-{doc_id}"
        # Deterministic by position, not random: the same document is the same
        # format on every run, so a re-generated screenshot is comparable to
        # the one before it.
        wants_pdf = pdf_share > 0 and (index % max(1, round(1 / pdf_share)) == 0)
        if wants_pdf and write_pdf(document, out / f"{stem}.pdf"):
            paths[doc_id] = f"{stem}.pdf"
            pdfs += 1
        else:
            write_markdown(document, out / f"{stem}.md")
            paths[doc_id] = f"{stem}.md"
    return {"documents": len(documents), "pdfs": pdfs, "paths": paths}


def judgements(claims: list[dict], paths: dict[int, str]) -> list[dict[str, Any]]:
    """Expert annotations, as the evidence pheasant's proof taxonomy wants.

    The label matters and is preserved. A SUPPORT rationale means the expert
    read that document and said it answers the claim — the strongest thing
    anybody can say about a retrieval result, and what pheasant records as
    ``explicit_accept``. A CONTRADICT rationale is *also* a positive for
    retrieval: finding the paper that refutes a claim is a correct answer to
    "what does the literature say", and treating it as a negative would train
    the region to hide disagreement.
    """

    out: list[dict[str, Any]] = []
    for claim in claims:
        for doc_id, rationales in claim["evidence"].items():
            path = paths.get(int(doc_id))
            if not path:
                continue
            labels = sorted({str(r.get("label") or "") for r in rationales})
            out.append(
                {
                    "query": claim["claim"],
                    "path": path,
                    "doc_id": int(doc_id),
                    "labels": labels,
                    "event_type": "explicit_accept",
                }
            )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--cache", default=str(REPO / ".benchmark-cache"))
    parser.add_argument("--out", required=True, help="Where to write the corpus directory.")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    print(f"{manifest['title']}  ({manifest['license']['claims']})")
    data_dir = download(manifest["source"]["url"], Path(args.cache))
    documents, claims = select(data_dir, manifest)
    written = materialize(documents, Path(args.out), float(manifest.get("pdf_share", 0.0)))
    judged = judgements(claims, written["paths"])

    index = {
        "name": manifest["name"],
        "documents": written["documents"],
        "pdfs": written["pdfs"],
        "claims": len(claims),
        "judgements": len(judged),
        "queries": [{"query": claim["claim"], "id": int(claim["id"])} for claim in claims],
        "evidence": judged,
        "source": manifest["source"],
        "license": manifest["license"],
    }
    manifest_path = Path(args.out).parent / "benchmark.json"
    manifest_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(
        f"  {written['documents']} documents ({written['pdfs']} as PDF), "
        f"{len(claims)} claims, {len(judged)} expert judgements"
    )
    print(f"  corpus:    {args.out}")
    print(f"  judgements: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
