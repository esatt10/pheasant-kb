# pheasant

pheasant is a **knowledge harness** for you and your agents. It turns your
sources (git repositories, folders, PDFs and Office documents, images and
audio, web pages, Google Drive and APIs) into one **knowledge graph** with
hybrid search and cited answers, served through a web UI, an HTTP API and MCP.

## Start here

pheasant runs as **pheasant-kb**: one command, a small set of containers.

=== "macOS / Linux"

    ```bash
    git clone https://github.com/esatt10/pheasant-kb && cd pheasant-kb && echo "OPENAI_API_KEY=sk-..." > .env && docker compose up -d
    ```

=== "Windows (PowerShell)"

    ```powershell
    git clone https://github.com/esatt10/pheasant-kb; cd pheasant-kb; Set-Content .env "OPENAI_API_KEY=sk-..."; docker compose up -d
    ```

Then open <http://127.0.0.1:8765>. The rest of the documentation uses this
stack as its reference.

<div class="grid cards" markdown>

-   :material-rocket-launch: **[Run pheasant-kb](pheasant-kb/index.md)**

    The one line, the token, adding your first sources, connecting an agent,
    and what each container does.

-   :material-tune: **[Configure it](pheasant-kb/configure.md)**

    Models, sources, host folders, retrieval, ingestion, and the features you
    can turn off.

-   :material-arrow-expand-all: **[Scale it up or down](pheasant-kb/scale.md)**

    Memory, CPU, storage, the vector store, workers and the indexer: which one
    is limited, and which number to move.

-   :material-wrench: **[Operate it](pheasant-kb/operate.md)**

    Status, upgrades, backups, token rotation and troubleshooting.

</div>

## Use it

| You want to… | Go here |
|---|---|
| Ask questions and browse sources in the web UI | [Ask your knowledge base](how-to/chat-and-ui.md) |
| Get longer answers, follow-ups and diagrams | [Answers, conversations and visuals](how-to/conversations-and-visuals.md) |
| Give a coding agent your knowledge base | [Attach to a coding agent](how-to/attach-to-coding-agent.md) · [MCP tools](mcp_tools.md) |
| Let agents remember | [Agent memory](how-to/agent-memory.md) |
| Add and tune sources | [Configure sources](how-to/sources.md) |
| Know whether retrieval is any good, and fix it | [Knowledge effectiveness](knowledge-effectiveness.md) · [Retrieval tuning](retrieval-tuning.md) |
| See every command, route and tool | [Interface matrix](reference/interfaces.md) |

## Other ways to run it

The same code runs as a single container with no database, broker or key, on
the host with `pip install pheasant-kb`, or across a Kubernetes cluster. See
[Other ways to run pheasant](other-deployments.md).

## pheasant and Synapse

pheasant is also the **region** half of **Synapse**, a federated knowledge-base
system whose router lives in
[pheasant-flock](https://github.com/esatt10/pheasant-flock). A region publishes
a semantic contract describing what it knows; the router uses it to decide
which regions to ask. It is entirely optional and off by default: a router-less
pheasant is a complete product. See
[Attach to a Synapse fleet](how-to/attach-to-synapse.md) and
[the Synapse suite](https://esatt10.github.io/pheasant-flock/).
