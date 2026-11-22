# pheasant documentation

pheasant is a **knowledge harness** for you and your agents. It turns your
sources (git repositories, folders, PDFs and Office documents, images and
audio, web pages, Google Drive, OneDrive and SharePoint, and APIs) into one
**knowledge graph** with hybrid search and cited answers, served through a web
UI, an HTTP API and MCP.

A region is a set of tiers (api, graph, indexer, workers, logger) over
PostgreSQL and NATS JetStream. Locally that is **pheasant-kb**, `compose.yaml`
at the repository root; in production, the same image and tiers on Azure
Container Apps.

## Start here

**macOS / Linux**

```bash
git clone https://github.com/esatt10/pheasant-kb && cd pheasant-kb && echo "OPENAI_API_KEY=sk-..." > .env && docker compose up -d
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/esatt10/pheasant-kb; cd pheasant-kb; Set-Content .env "OPENAI_API_KEY=sk-..."; docker compose up -d
```

Then open <http://127.0.0.1:8765>. The rest of the documentation uses this
stack as its reference.

| | |
|---|---|
| **[Run pheasant-kb](pheasant-kb/index.md)** | The one line, the token, your first sources, connecting an agent, what each container does |
| **[Configure it](pheasant-kb/configure.md)** | Models, sources, host folders, retrieval, ingestion, and the features you can turn off |
| **[Scale it](pheasant-kb/scale.md)** | Memory, CPU, storage, the vector store, workers and the indexer: which one is limited, and which number to move |
| **[Operate it](pheasant-kb/operate.md)** | Status, upgrades, backups, token rotation and troubleshooting |
| **[Deploy it](deployment.md)** | The tiers, production on Azure Container Apps, the generic fleet file, sharding, probes and storage |

## Use it

| You want to… | Go here |
|---|---|
| Ask questions and browse sources in the web UI | [Ask your knowledge base](how-to/chat-and-ui.md) |
| Get longer answers, follow-ups and diagrams | [Answers, conversations and visuals](how-to/conversations-and-visuals.md) |
| Keep composed files, reports and threads | [Keep what pheasant generates](how-to/artifacts.md) |
| Give a coding agent your knowledge base | [Attach to a coding agent](how-to/attach-to-coding-agent.md) · [MCP tools](mcp_tools.md) |
| Let agents record and recall facts | [Give your agents durable records](how-to/agent-records.md) |
| Add sources and keep them fresh | [Configure sources](how-to/sources.md) · [Sync schedules](how-to/sync-schedules.md) · [SharePoint and OneDrive](how-to/sharepoint-onedrive.md) |
| Index documents, images and audio well | [Document ingest](how-to/document-ingest.md) · [Structured documents](how-to/structured-documents.md) · [Images and audio](how-to/multimodal-ingest.md) · [OKF bundles](how-to/okf-bundles.md) |
| Know whether retrieval is any good, and fix it | [Knowledge effectiveness](knowledge-effectiveness.md) · [Retrieval tuning](retrieval-tuning.md) |
| Host a region for other people | [Host a region](how-to/hosted-regions.md) · [Roles](how-to/roles.md) · [Customer runbook](how-to/customer-runbook.md) |

## Understand it

| | |
|---|---|
| [Architecture](architecture.md) | How the tiers, the stores and the planes fit together |
| [The graph model](graph_model.md) | Node and edge types, stable ids |
| [Artifact records](artifact-records.md) | Records, validity, steering, compaction, isolation |
| [Observation and record formation](observation-and-formation.md) | The interaction ledger, the log tier, the formation rules |
| [Security](security.md) | The trust boundaries and the startup refusals that hold them |
| [Stress-test readiness](stress-test-readiness.md) | The capability contract and the go/no-go gates |

## Reference

| | |
|---|---|
| [Configuration](configuration.md) | Every setting, and the defaults table |
| [HTTP API](reference/http-api.md) | Every route |
| [MCP tools and resources](mcp_tools.md) | Every tool an agent can call |
| [Interface matrix](reference/interfaces.md) | Each capability on the CLI, HTTP, the UI and MCP |
| [Parquet export schema](reference/export-schema.md) | What an outside reader of `/exports` gets |
| [Connector SDK](reference/connector-sdk.md) | Writing a third-party source connector |
| [Troubleshooting](troubleshooting.md) | Symptoms and fixes |
