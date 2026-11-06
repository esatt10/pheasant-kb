import yaml

from tests.conftest import REPO_ROOT

SKILL = REPO_ROOT / ".agents" / "skills" / "pheasant-deploy" / "SKILL.md"
OPENAI_METADATA = SKILL.parent / "agents" / "openai.yaml"


def test_repository_deployment_skill_routes_blank_and_preset_workflows() -> None:
    text = SKILL.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])

    assert frontmatter["name"] == "pheasant-deploy"
    assert "Blank canvas" in text
    assert "Preset" in text
    assert "Never hand-write `pheasant.yaml`" in text
    assert "deploy/compose/docker-compose.advanced.yml" in text
    assert "deploy/compose/docker-compose.scale.yml" in text
    assert "memory_write" in text


def test_deployment_skill_has_discoverable_ui_metadata() -> None:
    metadata = yaml.safe_load(OPENAI_METADATA.read_text(encoding="utf-8"))
    assert metadata["interface"]["display_name"] == "Pheasant Deploy"
    assert "$pheasant-deploy" in metadata["interface"]["default_prompt"]


RETRIEVAL_SKILL = REPO_ROOT / ".agents" / "skills" / "pheasant-retrieval" / "SKILL.md"


def test_retrieval_skill_names_only_tools_the_server_publishes(tmp_path) -> None:
    """A skill that tells an agent to call a tool the server does not have is
    the readiness contract's stale-symbol bug, in prose. Every backticked
    snake_case call in the skill must be a registered MCP tool or prompt."""

    import asyncio
    import re

    import pytest

    pytest.importorskip("mcp")
    from pheasant.config.schema import PheasantConfig
    from pheasant.mcp_server.server import create_mcp_server

    text = RETRIEVAL_SKILL.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])
    assert frontmatter["name"] == "pheasant-retrieval"

    server = create_mcp_server(
        PheasantConfig.model_validate(
            {
                # Not the default /state, which only a root runner can create.
                "pheasant": {
                    "name": "skill",
                    "state_path": str(tmp_path / "state"),
                    "exports_path": str(tmp_path / "exports"),
                    "workspace_root": str(tmp_path),
                },
                "sources": [],
            }
        )
    )
    published = {tool.name for tool in asyncio.run(server.list_tools())}
    published |= {prompt.name for prompt in asyncio.run(server.list_prompts())}
    named = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)(?:\(|`)", text))
    tools = {
        name
        for name in named
        if name.split("_")[0]
        in {"ask", "search", "describe", "get", "explain", "list", "record", "seal", "use"}
    }
    assert {"search_context", "get_graph_neighbors", "get_graph_slice"} <= tools
    assert not tools - published, f"the skill names tools the server lacks: {tools - published}"

    metadata = yaml.safe_load((RETRIEVAL_SKILL.parent / "agents" / "openai.yaml").read_text())
    assert "$pheasant-retrieval" in metadata["interface"]["default_prompt"]


INGEST_SKILL = REPO_ROOT / ".agents" / "skills" / "pheasant-ingest" / "SKILL.md"
#: Fields of a tool's answer the skill names in backticks, which are not tools.
RESPONSE_FIELDS = {"sync_result"}
INGEST_VERBS = {
    "register",
    "edit",
    "submit",
    "sync",
    "start",
    "describe",
    "get",
    "acknowledge",
    "search",
}


def _standalone_server(tmp_path):
    from pheasant.config.schema import PheasantConfig
    from pheasant.mcp_server.server import create_mcp_server

    return create_mcp_server(
        PheasantConfig.model_validate(
            {
                "pheasant": {
                    "name": "skill",
                    "defaults": "standalone",
                    "state_path": str(tmp_path / "state"),
                    "exports_path": str(tmp_path / "exports"),
                    "workspace_root": str(tmp_path),
                },
                "sources": [],
            }
        )
    )


def test_ingest_skill_names_only_tools_the_server_publishes(tmp_path) -> None:
    """The waiting protocol is only as good as the tools it names."""

    import asyncio
    import re

    import pytest

    pytest.importorskip("mcp")
    text = INGEST_SKILL.read_text(encoding="utf-8")
    assert yaml.safe_load(text.split("---", 2)[1])["name"] == "pheasant-ingest"
    published = {tool.name for tool in asyncio.run(_standalone_server(tmp_path).list_tools())}
    named = {
        name
        for name in re.findall(r"`([a-z]+(?:_[a-z]+)+)(?:\(|`)", text)
        if name.split("_")[0] in INGEST_VERBS and name not in RESPONSE_FIELDS
    }
    assert {"describe_source", "get_index_queue", "get_ingest_status", "get_job"} <= named
    assert not named - published, f"the skill names tools the server lacks: {named - published}"
    metadata = yaml.safe_load((INGEST_SKILL.parent / "agents" / "openai.yaml").read_text())
    assert "$pheasant-ingest" in metadata["interface"]["default_prompt"]


def test_server_instructions_tell_every_agent_to_wait_for_the_index(tmp_path) -> None:
    """The skill reaches agents with this repository; the server's instructions
    reach every connected client, so the rule that matters most lives there too:
    a published sync is not an indexed one."""

    import pytest

    pytest.importorskip("mcp")
    instructions = _standalone_server(tmp_path).instructions or ""
    for phrase in ("queued", "describe_source", "last_indexed_at", "totals.documents"):
        assert phrase in instructions, phrase
