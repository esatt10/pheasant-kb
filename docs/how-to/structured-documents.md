# How to extract a taxonomy from structured documents

Books, standard operating procedures, standards and contracts already carry
their own outline — Part, Chapter, Article, Section, `§ 12.3`, `1.2.3`, `(a)`.
pheasant can extract that outline and use it as retrieval structure, so
"what does § 12.3 say about termination?" returns **§ 12.3**, not "the file
that mentions termination somewhere".

## Turn it on when you register the source

It is a **per-source** switch, because it is a claim about the source: *this
corpus is structured documentation.*

```yaml
sources:
  - name: contracts
    type: document_folder
    path: /workspace/contracts
    include: ["**/*.pdf", "**/*.docx"]
    taxonomy:
      enabled: true
```

Through the API:

```bash
curl -X POST localhost:8765/sources -H 'content-type: application/json' -d '{
  "name": "contracts", "type": "document_folder", "path": "/workspace/contracts",
  "include": ["**/*.pdf", "**/*.docx"],
  "taxonomy": {"enabled": true},
  "sync_now": true
}'
```

Or the one-field form, where it is a plain boolean:

```bash
curl -X POST localhost:8765/sources/quick-add -H 'content-type: application/json' \
  -d '{"target": "/workspace/contracts", "taxonomy": true}'
```

Or from an agent over MCP:

```
register_source(knowledge_base="...", name="contracts",
                source_type="document_folder", path="/workspace/contracts",
                taxonomy=True)
```

Then sync. It runs automatically on every sync from then on.

!!! tip "Or let each file decide"
    `taxonomy.enabled` is one answer for a whole source, and every rule runs on
    every file. `chunking.strategy: auto` decides per file instead: a bounded
    scan turns on only the heading rules a document actually uses, tells a
    numbered outline from a numbered list, scales the heading cap with the
    document's length, and packs short clauses together instead of making one
    chunk each. See [Dynamic chunking](dynamic-chunking.md).

## What you get

**Search tells you which section matched.** Every result carries
`heading_path`:

```bash
curl -X POST localhost:8765/search -H 'content-type: application/json' \
  -d '{"query": "governing law", "mode": "text"}' | jq '.results[0].heading_path'
```
```
"MASTER SERVICES AGREEMENT > Article IV Term and Termination > § 12.3 Governing Law"
```

**Chunks are the sections.** With `split_on_sections` (on by default once
taxonomy is enabled), a chunk *is* a section rather than a 4000-character
window that happens to straddle three of them. A section longer than
`chunking.max_chars` is still subdivided, and all its pieces keep the same
path.

**You can ask a question of one section.** `section` on `/search` matches the
breadcrumb, so cite it however you naturally would:

```bash
curl -X POST localhost:8765/search -H 'content-type: application/json' \
  -d '{"query": "termination", "section": "§ 12.3"}'
```

`§ 12.3`, `Article IV` and `Governing Law` all reach
`… > Article IV Term and Termination > § 12.3 Governing Law` — and naming a
parent returns everything nested under it, so `"section": "Article IV"` answers
from its subsections too. Available on MCP `search_context(section=...)` as well.
Graph hits (symbols, entities) are left out under a section filter: they are not
inside any document section. Use `GET /taxonomy` to browse the outline itself.

**The outline is browsable.**

```bash
curl 'localhost:8765/taxonomy?path=msa.pdf' | jq
```

```json
{"documents": [{"relative_path": "msa.pdf", "heading_count": 8,
  "tree": [{"number": null, "title": "MASTER SERVICES AGREEMENT", "kind": "caps",
    "children": [{"number": "Article IV", "title": "Term and Termination",
      "children": [{"number": "4.1", "title": "Initial Term", "children": []}]}]}]}]}
```

**The graph has it too.** `heading` nodes, `has_heading` from the document to
each section, and `contains` between a section and its subsections — so the
existing graph traversal walks the outline without knowing anything new. Each
node carries its parsed ordinal (`ordinal_parts`, `ordinal_series`), so a
section is queryable by citation rather than by wording.

**Numbering defects are reported.** `GET /taxonomy` returns an `issues` list per
document:

```json
{"kind": "gap", "series": "code", "after": "§ 12.3", "at": "§ 12.5", "missing": [4]}
{"kind": "duplicate", "series": "lettered", "after": "(b)", "at": "(b)"}
```

`gap`, `duplicate` and `out_of_order`. Only gaps *between observed members of
one series* count — a series starting at 3 is an excerpt, not a defect — and an
inserted `§ 12A` never creates one.

A series is identified by its **number**, not by where its sections ended up in
the tree: `§ 12.1`, `§ 12.2`, `§ 12.4` is one series even if an unnumbered
heading between them re-parents the tail, which is exactly when the gap is
easiest to miss. Top-level numbering is the exception — it has no prefix to
identify it — so it is grouped by parent, which is what makes `PART I` and
`PART II` each numbering their sections from 1 legal rather than a defect.

## What is recognised

| Rule | Matches | Level |
|---|---|---|
| `markdown` | `#` … `######` | 1-6 |
| `keyword` | `PART` `TITLE` `BOOK` `DIVISION` `SCHEDULE` `APPENDIX` `ANNEX` `EXHIBIT` | 1 |
| | `CHAPTER` `SUBPART` | 2 |
| | `ARTICLE` `SECTION` `RULE` `REGULATION` | 3 |
| | `CLAUSE` `STEP` `PARAGRAPH` | 4 |
| `code` | `§ 12.3`, `§§ 4` | 3+ |
| `numbered` | `1`, `1.2`, `1.2.3` | 3+ |
| `lettered` | `(a)`, `(iv)` | 6 |
| `caps` | `ALL CAPS HEADING LINE` | 2 |

A document may mix conventions and still nest, because the **ordinal** decides
the parent wherever there is one: `4.1` attaches to `ARTICLE IV` since `IV`
parses to `(4,)`, while `§ 12.3` refuses that Article — `(4,)` is not a prefix
of `(12, 3)` — and attaches above it instead. `§ 12A` is a sibling of `§ 12`,
not a child. A numbering that resumes after an interruption rejoins its own
parent, which pattern depth alone could not do.

A bare citation takes its caption from the next line, because legal drafting
splits them:

```
ARTICLE IV
Term and Termination
```
→ `Article IV Term and Termination`

Narrow the rules if a corpus only uses some, or if one is noisy for you:

```yaml
    taxonomy:
      enabled: true
      detect: ["markdown", "keyword", "code"]   # drop numbered/lettered/caps
```

## Honest limits

**Numbered lines are ambiguous.** `1. Introduction` in a standard is a section;
`1. Buy milk` in a note is a list item, and nothing in the line distinguishes
them. Length and punctuation filters catch the long, sentence-like cases, but
short list items will be read as sections. This is the reason the feature is
per-source: enable it where the structure is real.

**Letters that are also roman numerals.** A lone `(c)`/`(d)`/`(l)`/`(m)` is
read as the letter, a lone `(i)`/`(v)`/`(x)` as the numeral. Within a run the
surrounding items settle it — `(h)` then `(i)` counts 8, 9, and `(iv)` then
`(v)` counts 4, 5, because whichever reading *continues* the run wins. A run
that opens at `(i)` is still roman, which is the right default and the one
remaining ambiguity. Lettered ordinals never decide hierarchy, so the cost is at
most one spurious `issues` entry.

**`caps` is the noisiest rule.** A document with shouted emphasis will produce
spurious level-2 sections. Drop it from `detect` if that happens.

**It changes the index for that source.** `heading_path` is an FTS column at
BM25 weight 2.0, so filling it changes ranking, and `split_on_sections` changes
chunk boundaries. Both are improvements on structured corpora and both want a
deliberate `--mode full` re-sync.

## Tuning

| Want | Set |
|---|---|
| Labels for search, but no extra graph nodes | `graph_nodes: false` |
| Keep existing chunk boundaries, just label them | `split_on_sections: false` |
| Ignore deep subsections | `max_depth: 3` |
| Only the conventions your corpus uses | `detect: [...]` |

## Troubleshooting

**`GET /taxonomy` is empty.** The source needs `taxonomy.enabled: true` *and* a
sync after that was set. Check `config_json` in `GET /sources`.

**Everything is one section.** The document's headings are not being matched —
check that the format's text extraction works at all first (`heading_path` needs
text, so a PDF with no extractable text has no outline either; see
[document ingest](document-ingest.md)).

**Too many spurious sections.** Narrow `detect` — drop `caps` first, then
`numbered`.

**Sections are nested wrongly.** Check the `ordinal` on the heading in
`GET /taxonomy`. Nesting follows the ordinal when there is one, so a wrong
parent usually means the number was not parsed as expected (an unusual
separator, or a caption that swallowed the number).
