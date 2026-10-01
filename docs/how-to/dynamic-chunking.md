# Chunk each file the way it is written

By default every source is cut the same way: fixed windows of
`chunking.max_chars`, or one chunk per section when `taxonomy` is on. A code
file, a Slack channel, a spreadsheet and an 8,000-page tariff all get that one
shape. `chunking.strategy: auto` plans each file instead, from what it is and
how it is laid out, without a model and without measurable latency.

## Turn it on

Per source:

```yaml
sources:
  - name: tariffs
    type: document_folder
    path: /workspace/tariffs
    include: ["**/*.pdf", "**/*.docx"]
    chunking:
      strategy: auto        # fixed (default) | sections | auto
      max_chars: 2000       # a ceiling the plan never exceeds
      overlap_chars: 200    # likewise
```

Or for every source in the region, which is the better replacement for
`taxonomy_enabled: true` (that turns every heading rule on for every file,
code included):

```yaml
sync:
  source_processing:
    chunk_strategy: auto
```

**It is the default in every shipped fleet profile** — `deploy/compose/fleet.yaml`
(and the `scalable`/`pheasant-lab` answer files it is generated from) and
`deploy/kubernetes/scaled/configmap.yaml` — and not in any single-container
profile, which keeps `fixed`.

Changing the strategy re-indexes the affected sources once, on their next
sync. `fixed` (or the legacy default spelling `semantic`) is the old behaviour
byte for byte, so leaving it alone changes nothing.

| Strategy | What it does |
|---|---|
| `fixed` | Fixed windows, or one chunk per section with `taxonomy.enabled`. Unchanged. |
| `sections` | You say the source is structured: detect headings with its `taxonomy.detect` rules and pack sections up to `max_chars`. |
| `auto` | A plan per file, below. |

## What `auto` decides

**From the source type and extension, for free:**

| Profile | Files | Unit merged into chunks | Target / max chars |
|---|---|---|---|
| `code` | `.py`, `.ts`, `.go`, `.rs`, `.java`, ... | top-level block (a definition with its decorators and comment) | 1,500 / 3,000 |
| `config` | `.json`, `.yaml`, `.toml`, `.xml`, ... | blank-line block | 1,500 / 3,000 |
| `markdown` | `.md`, Markdown folders, Obsidian | `#` section | 1,800 / 3,000 |
| `memory` | memory sources | the whole record | 8,000 |
| `messages` | Slack, or any text shaped like a transcript | whole messages | 1,500 / 3,000 |
| `tabular` | `.xlsx` | rows, each chunk repeating the sheet name and header row | 1,500 / 3,000 |

**From a structural scan, for everything else** (PDF, Word, plain text, web
pages). The scan reads the first 32 KB and 48 evenly spaced 4 KB windows and
classifies each line with the taxonomy's own heading rules — ~3 ms on a 41M
character document, the same at any length. It then:

- turns on only the heading rules the document actually uses;
- counts `1.`, `2.`, `3.` as an outline only when the lines are *not*
  consecutive list items and either nest (`4.2`) or sit beside `ARTICLE` /
  `§` headings — so a numbered shopping list stays prose;
- uses ALL-CAPS lines as headings only when nothing else fires and they are
  rare;
- sizes chunks to the sections it saw: under ~800 characters a section,
  1,200-character chunks; under ~1,600, 1,600; longer, 2,000 with
  paragraph-level splits;
- with no headings at all, packs paragraphs to 2,000 characters.

`max_chars` and `overlap_chars` are ceilings on every profile, because those
are what you set to fit your embedding model.

## How chunks are cut

Units are merged in order until the next would pass the target. A section
becomes its own chunk once it reaches the minimum (a third of the target);
only smaller sections are folded into a neighbour, and the chunk is then
labelled with every section in it — `Article 4 > 4.1 Scope; 4.2 Term` — so the
`section` search criterion and the label's double BM25 weight still find each
one. Only a unit larger than the ceiling is split, at a paragraph break, then
a line break, then a sentence end, and overlap is applied only inside such a
split, never across two sections.

The packer never drops a line and never exceeds the ceiling; both are
property-tested.

## See a plan before you index

```bash
python -m pheasant.ingestion.chunk_plan contracts/master-services.pdf
python -m pheasant.ingestion.chunk_plan tariff.pdf --source tariffs -c pheasant.yaml
```

It prints the plan, the reason for it, and the chunks it would produce. After
indexing, the same plan is on the artifact's graph node as `chunk_plan`.

## What it costs and what it buys

Measured with `scripts/compare_chunking.py` (one 4-core host, SQLite, the
offline stub embedder; reproduce with the commands in its docstring).

**This repository's docs plus SciFact** (42 Markdown files; 395 abstracts, a
quarter as PDFs; 4,000/400 limits). SciFact's 60 claims carry expert
relevance judgements. Section lookup asks for 80 headings of the docs by
title; a hit is a returned chunk containing that heading.

| | fixed | fixed + taxonomy | auto |
|---|---|---|---|
| chunks | 600 | 1,104 | **929** |
| characters indexed | 1.35M | 1.29M | 1.29M |
| SciFact MRR@10, text | 0.908 | 0.906 | 0.906 |
| SciFact recall@10, text | 0.95 | 0.95 | 0.95 |
| section found first, text | 0.600 | 0.825 | **0.863** |
| section in top 5, text | 0.763 | 0.888 | 0.888 |
| section in top 5, hybrid | 0.675 | 0.863 | **0.888** |
| section found first, hybrid | 0.350 | **0.613** | 0.475 |

SciFact abstracts are short enough to be one chunk under every strategy, so
the judged numbers say only that `auto` costs nothing there. On structured
text it finds sections as well as one-chunk-per-section does, with 16% fewer
chunks. The one regression, hybrid rank 1, is measured with the stub
embedder, a bag-of-words hasher; confirm it against your real embedder before
reading anything into it.

**An 8,000-page tariff** (41M characters, 2,000/200 limits):

| | fixed | fixed + taxonomy | auto |
|---|---|---|---|
| index time | 25.0 s | 25.7 s | 27.6 s |
| chunks | 23,630 | 24,210 | 26,669 |
| characters indexed (≈ embedding tokens × 4) | 46.0M | 45.7M | **43.0M** |
| headings kept | 0 | 2,000 (capped) | **10,720** |

`auto` embeds 6% fewer characters, because overlap is applied only inside
split sections, in 13% more chunks. Against a tokens-per-minute limit that
is a little faster; the extra 2.6 s is writing 10,720 heading nodes. The
heading cap scales with document length under `sections`/`auto` (one heading
per 2,000 characters, at least 2,000), so the whole outline is kept rather
than the first 2,000 headings and one huge remainder.

## Workers

Remote preparation now carries headings and the chunk plan, so taxonomy-
enabled and `auto` sources are prepared by the worker fleet instead of being
refused to the indexer. A PDF longer than
`sync.concurrency.remote_worker_pdf_pages_per_task` (500) has its text read by
all the workers in page ranges, over gRPC or HTTP alike, and is then planned
and chunked on the indexer, which needs the whole document. A worker on a different planner version is refused for
that file, and the indexer prepares it itself.

## Re-indexing

The planner's rules are versioned (`PLANNER_VERSION`) and the version is part
of the source fingerprint for `sections` and `auto`. An unchanged file always
gets the same plan and the same chunks; a planner change re-indexes exactly
the sources that use it.
