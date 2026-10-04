# Connector SDK

Add a new source type to pheasant from your own package — no fork required
(Product Framework Step 31.1). A connector plugin is a
`SourceConnector` subclass published under a `pheasant.connectors` entry
point; the entry-point **name** is the `sources[].type` string users write
in `pheasant.yaml`.

## The shape of a connector package

The canonical example lives in the repo at
`tests/fixtures/pheasant-connector-example/`:

```toml
# pyproject.toml
[project]
name = "pheasant-connector-example"
dependencies = ["pheasant-kb>=0.3"]

[project.entry-points."pheasant.connectors"]
staticdir = "pheasant_connector_example:StaticDirConnector"
```

```python
from pheasant.sync.connectors import ConnectorItem, ConnectorPayload, SourceConnector

class StaticDirConnector(SourceConnector):
    connector_type = "staticdir"

    def list_items(self) -> list[ConnectorItem]: ...   # deterministic identities
    def read_item(self, item) -> ConnectorPayload: ... # stable bytes
    def checkpoint_from_items(self, items): ...        # incremental cursor
```

Once the package is installed, users configure it like any built-in source:

```yaml
sources:
  - name: team-wiki
    type: staticdir          # ← your entry-point name
    path: /srv/wiki-export
```

Config load never requires the plugin to be installed — an unknown `type`
is carried as a plugin source type and resolved when a sync dispatches. A
missing plugin fails the sync with an error naming the type and every
installed connector plugin.

## The contract your connector must honor

The four product pillars apply to plugins exactly as to built-ins:

1. **Deterministic** — `list_items()` returns stable, unique `identity`
   values; no LLM calls, no wall-clock-dependent output.
2. **Idempotent** — unchanged content re-reads to identical bytes (the
   engine's sha256 skip does the rest), or `read_item` raises
   `ItemNotModified`.
3. **Incremental** — `checkpoint_from_items()` returns a JSON-serializable
   `(cursor, high_watermark)`; `begin_sync("incremental")` sees the previous
   checkpoint, `full`/`repair` ignore it.
4. **State discipline** — checkpoints persist only through the provided
   `StateStore` (`set_checkpoint`/`get_checkpoint`); never write your own
   files into `/state`.
5. **Say whether your listing is complete.** Set `complete_listing = True`
   only if `list_items()` returns *everything* the source holds on every
   call. An incremental sync then removes whatever it indexed before and no
   longer sees. Leave it `False` (the default) if you list only what changed,
   such as new messages or a feed's latest page: pheasant then never treats
   an item's absence as a deletion, and a `full` sync is how removals land.

## The conformance bar

Hold your connector to the same harness the built-ins pass. In your
package's test suite:

```python
from pheasant.testing import ConnectorConformance

class TestMyConnectorConformance(ConnectorConformance):
    def make_connector(self, tmp_path, state):
        root = tmp_path / "content"
        root.mkdir()
        (root / "sample.txt").write_text("hello")
        source = SourceConfig(name="demo", type=PluginSourceType("mytype"), path=root)
        return MyConnector(source, state)
```

That one subclass runs the full contract: declared `connector_type`,
healthy `validate()`, deterministic + unique identities, stable payloads,
checkpoint round-trip through the state store, and `full`-mode checkpoint
bypass.

## Certified connectors (Step 31.7)

"Certified" means the connector passes `ConnectorConformance` and its test
suite runs fully offline against recorded fixtures. Every first-party
connector clears the same bar — the harness subclass ships in each one's
tests:

| Type | Source | Incremental mechanism | ACL capture (Phase 32) |
|---|---|---|---|
| `gdrive` | Google Drive docs + text files | per-file `modifiedTime`/`md5Checksum` | owners + `shared` flag |
| `staticdir` | Example plugin (`tests/fixtures/pheasant-connector-example/`) | mtime watermark | — |

For your own package, copy the example package's shape — it now includes
the canonical certification test
(`tests/fixtures/pheasant-connector-example/tests/test_conformance.py`):
subclass `ConnectorConformance`, provide `make_connector`, run pytest.
Publication of the example as a standalone PyPI/cookiecutter template is
release-channel work.

## Programmatic registration (embedders, tests)

```python
from pheasant.sync.connector_registry import register_connector_class

register_connector_class("mytype", MyConnector)   # wins over entry points
```

`list_connector_types()` reports everything resolvable;
`reset_connector_registry()` clears programmatic registrations and the
entry-point cache (test isolation).
