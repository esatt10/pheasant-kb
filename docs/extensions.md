# Pheasant extension protocol

An extension is an operator-installed distribution with a contribution in the Python `pheasant.extensions` entry-point group. Pheasant discovers these distributions at process startup. A connector that only reads content can continue using the narrower `pheasant.connectors` contract.

## Backend contract

Declare an entry point whose name matches the returned `Extension.id`.

```toml
[project.entry-points."pheasant.extensions"]
example = "example_extension.plugin:extension"
```

The entry-point function returns `pheasant.extensions.Extension(id="example", api_version=1, ...)`. Each optional contribution can be added independently.

| Contribution | Host behavior |
| --- | --- |
| `register_http`, `http_permissions` | Register routes on the region API. Declare their exact `(method, path template)` permission. Pheasant authenticates requests before dispatch and rejects conflicting or unclassified extension routes during startup. |
| `register_mcp`, `mcp_permissions` | Register tools with `@mcp.tool()` and the supplied `@anticipated` decorator. The host applies the operation's declared permission to calls and discovery. Use the same service functions as HTTP. |
| `sources` | Declare the type, connector factory, display name, registration help and optional per-source capabilities. The host calls the connector through the existing sync queue and indexing pipeline. Store validated settings in `SourceConfig.extension_settings`. A declaration can name an old settings key for read compatibility. |
| `commands` | Extend `pheasant` with an argparse subcommand, help and handler. |
| `migrations` | Append immutable migrations, each a tuple of PostgreSQL statements. The host applies each transaction under the schema lock and records its revision in `pheasant_schema_meta`. Use extension-prefixed table names. |
| `ui_routes` | Declare exact top-level UI paths for document navigation and telemetry. Authenticated `GET /extensions` returns installed IDs, paths and the protocol version. |
| `chat_commands` | Answer a first-word chat keyword (`ChatCommand(name, summary, example, run, aliases)`), as `@harness` does. The chat reads it like `@remove`: first word only, nothing searched, no model called on Pheasant's side. `run(services, ChatCommandRequest)` receives the words after the keyword and where the reader is (principal, conversation, source scope, open document and folder), runs under the identity the transport bound, and returns `text` (the answer, Markdown) plus anything else, which the answer carries as `extension`. A `ValueError` is shown as the answer; any other error is reported as an extension failure. The keyword appears in the chat's keyword reference under *Extensions*. A built-in keyword always wins a name an extension also declares, and two extensions cannot declare one name. |

A source extension may opt into artifact-owned graph provenance by declaring a `Provenance` type and ID prefix. Return curated dictionaries under the connector payload's `extension_provenance` key. Each record has a stable `id`, a `label`, and `field_keys`. The host binds the evidence to the artifact and source, creates `derived_from` edges, enforces the artifact's ACL, and removes stale references on reindex. Choose a unique node type and prefix. List safe, nonsecret sync outcome keys in `public_sync_details` to expose them through `wait_for_sync`.

An installed connector can return `ConnectorPayload.normalized_text` from its own isolated parser and supply the fetched HTML as `content`. The host uses the supplied text for chunks and handles HTML snapshots through the standard web viewer. During sync, the connector can checkpoint captures in the host's content-addressed `media_store`. To connect a curated artifact to other indexed documents from the same source, list the inputs first and set `extension_inputs` in the curated payload. Map each input's relative path to its field keys, using an empty list for a research input with no field citation. The host links existing input nodes with `derived_from` and retracts stale links when the curated document is reindexed.

## Browser and image integration

Repository extensions can add `extensions/<id>/ui/extension.tsx` with a default `UiExtension` export. Contribute navigation, page components, source editor components, source-row presentation, optional Taxonomy folder details, and `adminTabs` (`{id, label, permission, Panel}`): tabs on the Admin page after the region's own, each offered only to a caller with its permission (gate it the way its server routes are gated). Use the host's authenticated HTTP client, theme tokens and shared components through `@pheasant/ui`. A page becomes visible when the bundle includes the extension and region discovery confirms the matching ID and paths.

Set the comma-separated `PHEASANT_BUILD_EXTENSIONS` build argument to install selected distributions and build their browser contributions into a Pheasant image. The default image builds without optional extensions. Use the same selected packages in every Pheasant tier that reads or writes the region. For local development, install the distribution with pip and set the same variable when running Vite. Backend-only installs expose HTTP, MCP, CLI and sync without a rebuilt browser bundle.

Extension Python and browser code runs with host trust. Install from operator-controlled packages and build inputs. Run untrusted content processing in a separate restricted worker. GTM uses a networkless parser sidecar and a confined worker per request. A normal extension uninstall leaves database tables and indexed artifacts in place. The connector and extension interfaces return when the package is reinstalled.

## Reference implementation

[GTM](../extensions/gtm) supplies each contribution. The [GTM Compose overlay](../deploy/compose/docker-compose.gtm.yml) builds the extension-enabled host image and the parser. The [GTM runbook](how-to/gtm-research.md) covers registration, sync and export.

The [agentic runtime harness](../extensions/harness) is a second extension built on the same contributions (HTTP, MCP, CLI, migrations and a UI route, no source type): durable, verified runs over registered places to look. Its README maps each part of the production-readiness spec to code and to an acceptance test. It can also work through the region's connected accounts (GitHub, Google Drive, Microsoft 365), but only within a per-connection policy an operator sets: which actions (`list`, `read`, `write`; never `delete` or `share`), on which resources, how much per run, and whether a write waits for approval. Its page is a workspace: attach files (kept in the `uploads` source too), pick a repository, branch and folder, and keep the verified result as a Pheasant artifact or as one commit with a pull request.
