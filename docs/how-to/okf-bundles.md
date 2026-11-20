# Open Knowledge Format (OKF) bundles

[OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
(v0.2) is Google's format for curated knowledge: a directory of Markdown
files with YAML frontmatter. Each non-reserved `.md` file is a **concept**
with a required `type`; `index.md` lists a directory; `log.md` records dated
changes; links and a few frontmatter fields relate concepts to each other.

pheasant supports OKF **implicitly**. There is nothing to register: add the
folder, repository, Obsidian vault or connector that holds a bundle exactly as
you would any document collection, and the bundle is detected while it is
indexed.

```yaml
sources:
  - name: finance-knowledge
    type: document_folder      # or markdown_folder, repository, …
    path: /data/finance-okf
```

A collection that holds no bundle produces no OKF node or edge, so its graph
is exactly what it was without this feature.

## How a bundle is detected

Parsing happens per file and is deterministic (no model, no network): a
Markdown file whose frontmatter has a non-empty string `type` is read as a
concept, an `index.md` with no frontmatter (or with `okf_version`) as a
listing, and a `log.md` with ISO-dated headings as a log. Whether a
**directory** is a bundle is then decided once the whole source is indexed:

| Detection | Rule |
|---|---|
| `explicit` | A directory whose `index.md` declares `okf_version`. |
| `index` | A directory whose `index.md` is an OKF listing and whose subtree is conformant. |
| `conformant_tree` | No listing, but a conformant subtree corroborated by a key only an OKF producer writes (`sources`, `generated`, `verified`, `stale_after`, `resource`, `runtime`, `executor`) or by a `log.md`. |

A subtree is *conformant* when it holds at least two concepts and at least
80% of its Markdown is concepts. Repository boilerplate (`README.md`,
`CHANGELOG.md`, …) is ignored. The rule is not "100%" because a bundle shipped
as a git repository usually carries a README, and the spec says to treat
everything beyond the required `type` as soft guidance. `type:` on its own is
never enough, because Hugo uses it to route pages too.

The shallowest qualifying directory wins and nothing nested in it is a second
bundle, so one source can hold several sibling bundles.

For a directory with an `index.md`, the share is measured over **what that
index lists**: the folders and files its entries name, plus the index's own
folder. A repository that *is* a bundle (a root `index.md` listing `metrics/`,
`tables/` ...) usually also carries developer docs, and measured over
everything under the root they would sink it below 80% and leave only
fragments as bundles. Hidden folders (`.github/`, `.changeset/`) are a
repository's tooling and count either way.

## Where a bundle can come from

Any source that indexes Markdown: a folder, a repository (with the bundle at
its root or in a subfolder), a cloud drive, or an upload. An upload is
detected the same way whichever form it takes:

- **An archive** of the bundle, ZIP or tar (`.tar`, `.tar.gz`, `.tgz`,
  `.tar.bz2`, `.tar.xz`): its members keep their paths inside the archive
  (`acme.tgz/acme/metrics/index.md`).
- **A folder**, dragged onto the drop zone or picked with **Choose a folder**,
  or files sent with their relative paths (`POST /sources/upload` with each
  part's filename set to `acme/metrics/index.md`, or MCP `submit_documents`):
  the folder structure is kept. Before this, an upload kept only file names,
  so three `index.md` files became `index.md`, `index-2.md` and `index-3.md`
  and the bundle could not be seen. Indexing Google's
whole `okf/` directory finds the four sample bundles and leaves the spec,
README and prompt files alone.

## What the graph gets

| OKF construct | Graph |
|---|---|
| The bundle | `okf_bundle` node, `source contains okf_bundle`; carries type, trust and status counts |
| `type` | `okf_type` hub per type, `bundle contains okf_type contains concept` (with `concept_id`, `status`, `trust_tier`) |
| `tags` | `tag` nodes, `concept tagged_with tag` |
| Body link | `concept links_to concept` (`relation: body_link`), beside the resolver's `references` edge |
| `sources[]` | `concept derived_from target` with `source_key`, `author`, `usage_count`, `last_modified`, `usage_window` and `citations` (footnotes citing that `id`); URLs and scope descriptors become shared `external_reference` stubs |
| Attested Computation | `executed_by` (with `receipt`), `attested_by`, `computed_by` |
| `index.md` | `links_to` with `relation: index_entry` and the entry's description; a directory target resolves to its `index.md` |
| `log.md` | `links_to` with `relation: log_entry`, every dated entry and `last_date` |
| `status: deprecated` | `current supersedes deprecated` when a deprecated concept links to exactly one current concept of its own type |

Every concept artifact also carries its parsed frontmatter as an `okf`
attribute: title, description, resource, status, `stale_after`, `generated`,
`verified`, the derived trust tier (`unverified`, `machine-confirmed` or
`human-reviewed`, §5.3), sources, and the computation contract. Graph search
matches on these. Timestamps are stored exactly as written. Staleness
(`now >= stale_after`) is **not** stored, because it changes with the clock
rather than the corpus and would move the content-addressed graph generation.

Paths resolve relative to the linking document first and to the bundle root
second. A leading `/` is bundle-relative (§6.1), and the spec's own examples
write `references/skills/run-on-bq.md` from inside `computations/`. A link
that leaves the bundle is not a bundle relationship, and a broken link is
counted on the bundle node (`broken_links`) rather than rejected.

## What its nearest index says, at retrieval

An index is where a bundle's curator records what a document's own
frontmatter has not caught up with: an entry filed under **Deprecated**,
"superseded by ...", "needs review", "updated 2026-07-01". Every Markdown
document in a bundle has one `indexed_by` edge to its **nearest** index, the
closest `index.md` in its own folder or above. The edge says whether that
index lists the document and what the entry states, read by fixed rules with
no model.

When a search or an answer retrieves a passage from a bundle, it follows that
edge (one hop) and the hit carries an `okf` block:

| Field | Meaning |
|---|---|
| `index`, `levels` | The nearest index, and how many folders up it is |
| `listed`, `index_says`, `index_text`, `stated_date` | Whether it lists the document, what the entry states (`superseded`, `deprecated`, `stale`, `draft`, `current`), its words, and a date it gives |
| `status`, `stale_after`, `past_stale_after` | The document's own frontmatter, with `stale_after` checked against the clock now |
| `verdict` | `superseded`, `deprecated`, `stale`, `draft`, `unlisted` or `current`. The index comes first; a passed `stale_after`, or an index date later than the document's last review, makes it `stale` |
| `conflict`, `note` | Whether the index and the document disagree, and one sentence saying so |

An answer's prompt carries the `verdict` and `note` under the passage
(`OKF index check: superseded. metrics/index.md lists it as superseded: ...`),
so the answer can say a source has been replaced rather than quote it as
current. Only hits on `.md` files are looked up, in one read per search, so a
region without bundles pays nothing. The verdict is computed when it is read
and never stored, because it changes with the clock.

## What it looks like

Google's `acme_retail` sample bundle, indexed as an ordinary
`document_folder`. These are only the edges the OKF pass drew:

![OKF-derived edges for acme_retail](../assets/okf/acme-retail-okf-edges.png)

In pheasant's Graph view, choose **Columns** from the layout menu to see a
bundle laid out the same way: bundle, concept types, concepts, listings and
references, then tags. Provenance is drawn in red and links in blue, with a
key naming every relationship colour. See
[Reading the graph](chat-and-ui.md#the-columns-layout). Concentric stays the
default.

![acme_retail in the Columns layout](../assets/ui/graph-columns-okf.png)

The same region in the Force layout, with symbols and directories hidden.
The bundle is the teal hexagon and the concept types are the amber ones:

![acme_retail in the pheasant Graph view](../assets/okf/acme-retail-ui-graph.png)

## Operating notes

- **Incremental and idempotent.** The pass runs per source after each sync.
  It walks only that source's artifacts and applies its plan as a diff, so an
  unchanged bundle writes nothing and leaves the graph generation alone. An
  edit retracts exactly the edges it removed.
- **Turning it off.** `graph.okf_bundles: false` retracts the OKF structure on
  each source's next sync. It removes only edges the OKF pass drew, never a
  parallel `references` edge.
- **An already-indexed bundle** gains its OKF structure the next time its
  files are read. Run `pheasant sync --source <name> --mode full` once to get
  it immediately.
- **Worker fleets.** The per-file reading travels with the prepared artifact.
  A worker older than this feature is refused for `.md` files only, and those
  files are prepared on the indexer instead.
